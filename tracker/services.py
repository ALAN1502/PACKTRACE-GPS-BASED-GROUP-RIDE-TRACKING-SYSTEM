from datetime import timedelta
from django.utils import timezone
from django.conf import settings
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from .models import RideGroup, RideMember, LocationLog

import math
import colorsys

DISCONNECT_THRESHOLD = getattr(settings, 'DISCONNECT_THRESHOLD_SECONDS', 45)
DEVIATION_THRESHOLD = getattr(settings, 'ROUTE_DEVIATION_THRESHOLD_METERS', 500)
RIDER_COLOR_PALETTE = (
    '#4C7EFF', '#E5595E', '#3ECF8E', '#F2A93B',
    '#B98CE0', '#4FD0E0', '#F0D64E', '#F07CB5',
)


def get_rider_color(rider_number):
    """Return a deterministic ride color, keeping the first eight distinct."""
    number = max(1, int(rider_number or 1))
    if number <= len(RIDER_COLOR_PALETTE):
        return RIDER_COLOR_PALETTE[number - 1]
    hue = ((number - len(RIDER_COLOR_PALETTE) - 1) * 0.61803398875) % 1
    red, green, blue = colorsys.hls_to_rgb(hue, 0.62, 0.76)
    return '#{:02X}{:02X}{:02X}'.format(round(red * 255), round(green * 255), round(blue * 255))

def haversine(lat1, lon1, lat2, lon2):
    """Calculate the great-circle distance between two points on the Earth surface."""
    R = 6371000  # radius of Earth in meters
    phi_1 = math.radians(lat1)
    phi_2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = math.sin(delta_phi / 2.0)**2 + \
        math.cos(phi_1) * math.cos(phi_2) * \
        math.sin(delta_lambda / 2.0)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

def check_route_deviation(member, lat, lng):
    """
    Checks if the rider has deviated from the planned route.
    If they have, sets route_deviation=True and returns a notification payload.
    """
    if not lat or not lng:
        return None
        
    group = member.group
    waypoints = group.planned_route
    if not waypoints:
        return None  # No route planned, so cannot deviate
        
    min_dist = float('inf')
    for wp in waypoints:
        # Support both [lat, lng] array format or {"lat": lat, "lng": lng} dict format
        wp_lat = wp[0] if isinstance(wp, (list, tuple)) else wp.get('lat')
        wp_lng = wp[1] if isinstance(wp, (list, tuple)) else wp.get('lng')
        
        if wp_lat is not None and wp_lng is not None:
            dist = haversine(lat, lng, float(wp_lat), float(wp_lng))
            if dist < min_dist:
                min_dist = dist
                
    # If the closest point on the route is further than the threshold, they deviated
    if min_dist > DEVIATION_THRESHOLD:
        if not member.route_deviation:
            member.route_deviation = True
            member.save(update_fields=['route_deviation'])
            
            return {
                'type': 'route_deviation',
                'rider_id': member.user.id,
                'name': member.user.get_full_name() or member.user.username,
                'message': f"Rider {member.user.get_full_name() or member.user.username} has deviated from the planned route.",
                'latitude': lat,
                'longitude': lng,
                'distance_meters': int(min_dist)
            }
    else:
        # They are back within range of the route
        if member.route_deviation:
            member.route_deviation = False
            member.save(update_fields=['route_deviation'])
            
    return None


def calculate_or_validate_speed(member, lat, lng, provided_speed, now):
    """
    Validates provided speed from GPS hardware.
    If unavailable, calculates approximate speed dynamically using the
    Haversine distance from the last known location and the time elapsed.
    """
    if provided_speed is not None:
        speed = float(provided_speed)
        if speed < 0: return 0.0
        if speed > 300: return 300.0
        return speed
        
    last_log = member.location_logs.order_by('-timestamp').first()
    if not last_log:
        return 0.0
        
    distance_meters = haversine(last_log.latitude, last_log.longitude, lat, lng)
    time_diff_seconds = (now - last_log.timestamp).total_seconds()
    
    if time_diff_seconds <= 0:
        return 0.0
        
    # m/s to km/h
    calc_speed = (distance_meters / time_diff_seconds) * 3.6
    
    if calc_speed < 0: return 0.0
    if calc_speed > 300: return 300.0
    return calc_speed


def check_and_update_disconnected_riders(group=None):
    """
    Inspects active riders across active groups.
    If (current_time - last_seen > DISCONNECT_THRESHOLD_SECONDS):
      - is_connected = False
      - status = 'DISCONNECTED'
      - Broadcasts 'rider_disconnected' event with last known coordinates to the group channel.
    """
    now = timezone.now()
    cutoff_time = now - timedelta(seconds=DISCONNECT_THRESHOLD)
    channel_layer = get_channel_layer()

    query = RideMember.objects.filter(
        status='ACTIVE',
        is_connected=True,
        group__status='ACTIVE'
    )
    if group:
        query = query.filter(group=group)

    # Find riders who haven't sent coordinates within threshold
    stale_members = list(query.filter(last_seen__lt=cutoff_time) | query.filter(last_seen__isnull=True))
    disconnected_list = []

    for member in stale_members:
        member.is_connected = False
        member.status = 'DISCONNECTED'
        member.save(update_fields=['is_connected', 'status'])

        latest_loc = member.location_logs.first()
        member_color = get_rider_color(member.rider_number)
        payload = {
            'type': 'rider_disconnected',
            'rider_id': member.user.id,
            'member_id': member.id,
            'rider_number': member.rider_number,
            'color': member_color,
            'role': member.get_role_display(),
            'vehicle': getattr(getattr(member.user, 'profile', None), 'ride_type', 'Motorcycle'),
            'name': member.user.get_full_name() or member.user.username,
            'last_latitude': latest_loc.latitude if latest_loc else None,
            'last_longitude': latest_loc.longitude if latest_loc else None,
            'last_seen': member.last_seen.isoformat() if member.last_seen else None
        }

        disconnected_list.append(payload)

        # Broadcast to Channels group room
        if channel_layer:
            try:
                async_to_sync(channel_layer.group_send)(
                    f"ride_group_{member.group_id}",
                    {
                        'type': 'broadcast_rider_status',
                        'message': payload
                    }
                )
            except Exception:
                pass

    return disconnected_list


def handle_rider_reconnect(member):
    """
    Called when a previously disconnected rider sends a GPS update or reconnects.
    - is_connected = True
    - status = 'ACTIVE'
    - Broadcasts 'rider_reconnected' event.
    """
    was_disconnected = (member.status == 'DISCONNECTED' or not member.is_connected)
    now = timezone.now()
    member.is_connected = True
    member.status = 'ACTIVE'
    member.last_seen = now
    member.save(update_fields=['is_connected', 'status', 'last_seen'])

    if was_disconnected:
        member_color = get_rider_color(member.rider_number)
        latest_loc = member.location_logs.first()
        channel_layer = get_channel_layer()
        if channel_layer:
            try:
                async_to_sync(channel_layer.group_send)(
                    f"ride_group_{member.group_id}",
                    {
                        'type': 'broadcast_rider_status',
                        'message': {
                            'type': 'rider_reconnected',
                            'rider_id': member.user.id,
                            'member_id': member.id,
                            'rider_number': member.rider_number,
                            'color': member_color,
                            'role': member.get_role_display(),
                            'vehicle': getattr(getattr(member.user, 'profile', None), 'ride_type', 'Motorcycle'),
                            'name': member.user.get_full_name() or member.user.username,
                            'status': 'ACTIVE',
                            'is_connected': True,
                            'latitude': latest_loc.latitude if latest_loc else None,
                            'longitude': latest_loc.longitude if latest_loc else None,
                            'speed': round(latest_loc.speed, 1) if latest_loc else None,
                            'timestamp': now.isoformat()
                        }
                    }
                )
            except Exception:
                pass
