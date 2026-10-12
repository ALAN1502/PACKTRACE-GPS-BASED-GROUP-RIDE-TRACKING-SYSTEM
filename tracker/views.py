import re
import json
import math
from decimal import Decimal, InvalidOperation
from datetime import timedelta
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login as auth_login, logout as auth_logout, authenticate
from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.http import JsonResponse, HttpResponseForbidden
from django.utils import timezone
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from .models import RiderProfile, RideGroup, RideMember, LocationLog, SOSAlert
from .decorators import admin_required
from .routing_service import RouteServiceError, calculate_osrm_route

def is_valid_phone(phone_str):
    """
    Validates phone number format allowing country codes, spaces, dashes and at least 10 digits.
    """
    if not phone_str:
        return False
    digits = re.sub(r'\D', '', phone_str)
    return 10 <= len(digits) <= 15


def is_valid_coordinate_pair(latitude, longitude):
    return math.isfinite(latitude) and math.isfinite(longitude) and -90 <= latitude <= 90 and -180 <= longitude <= 180


def resolve_dashboard_group_for_user(user, requested_pack_id=None, groups=None):
    """
    Resolve the correct pack for the dashboard so the rider connects to the right
    active ride and never treats a historical/completed ride as live dashboard state.
    """
    if groups is None:
        groups = RideGroup.objects.filter(
            status__in=['READY', 'ACTIVE'],
        ).filter(
            Q(created_by=user) | Q(members__user=user),
        ).distinct().order_by('-created_at')

    if requested_pack_id:
        selected = groups.filter(id=requested_pack_id, status__in=['READY', 'ACTIVE']).first()
        if selected:
            return selected

    member_group = groups.filter(members__user=user, members__status='ACTIVE').order_by('-created_at').first()
    if member_group:
        return member_group

    active_group = groups.filter(status='ACTIVE').first()
    if active_group:
        return active_group

    return groups.first()


def parse_planned_route(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return None
    if not isinstance(value, list) or not 2 <= len(value) <= 5000:
        return None

    route = []
    for point in value:
        try:
            if isinstance(point, dict):
                latitude = float(point.get('lat', point.get('latitude')))
                longitude = float(point.get('lng', point.get('longitude')))
            else:
                latitude, longitude = map(float, point[:2])
        except (TypeError, ValueError, IndexError):
            return None
        if not is_valid_coordinate_pair(latitude, longitude):
            return None
        route.append([latitude, longitude])
    return route


@login_required(login_url='login')
@require_POST
def osrm_route_api(request):
    try:
        payload = json.loads(request.body.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse({'success': False, 'error': 'A valid JSON coordinate request is required.'}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({'success': False, 'error': 'A valid JSON coordinate request is required.'}, status=400)

    parsed_coordinates = {}
    for field in ('start_lat', 'start_lng', 'destination_lat', 'destination_lng'):
        value = payload.get(field)
        if isinstance(value, bool) or value is None:
            return JsonResponse({'success': False, 'error': f'{field} must be a numeric coordinate.'}, status=400)
        try:
            parsed_coordinates[field] = float(value)
        except (TypeError, ValueError, OverflowError):
            return JsonResponse({'success': False, 'error': f'{field} must be a numeric coordinate.'}, status=400)
        if not math.isfinite(parsed_coordinates[field]):
            return JsonResponse({'success': False, 'error': f'{field} must be a finite coordinate.'}, status=400)

    if (not is_valid_coordinate_pair(parsed_coordinates['start_lat'], parsed_coordinates['start_lng'])
            or not is_valid_coordinate_pair(parsed_coordinates['destination_lat'], parsed_coordinates['destination_lng'])):
        return JsonResponse({'success': False, 'error': 'Coordinates are outside valid latitude/longitude ranges.'}, status=400)

    try:
        result = calculate_osrm_route(
            **parsed_coordinates,
            referer=request.build_absolute_uri('/'),
        )
    except RouteServiceError as error:
        return JsonResponse({'success': False, 'error': str(error)}, status=error.status)
    return JsonResponse(result)

def register_view(request):
    """
    Handles user registration creating a real Django User account with hashed password
    and associated RiderProfile with phone, ride_type, and location_consent.
    """
    if request.user.is_authenticated:
        return redirect('dashboard')

    if request.method == 'POST':
        fullname = request.POST.get('fullname', '').strip()
        email = request.POST.get('email', '').strip().lower()
        phone = request.POST.get('phone', '').strip()
        ride_type = request.POST.get('vehicle', request.POST.get('ride_type', '')).strip()
        password = request.POST.get('password', '')
        confirm = request.POST.get('confirm', '')
        terms = request.POST.get('terms') or request.POST.get('location_consent')

        valid_ride_types = dict(RiderProfile.RIDE_TYPE_CHOICES).keys()
        if ride_type not in valid_ride_types:
            ride_type = ''

        errors = []
        if not fullname:
            errors.append('Full name is required.')
        if not email or '@' not in email:
            errors.append('A valid email address is required.')
        elif User.objects.filter(email__iexact=email).exists():
            errors.append('An account with this email already exists.')
        if not phone or not is_valid_phone(phone):
            errors.append('Enter a valid phone number (at least 10 digits).')
        if not ride_type:
            errors.append('Select a ride type.')
        if not password or len(password) < 8:
            errors.append('Password must be at least 8 characters long.')
        elif password != confirm:
            errors.append('Passwords do not match.')
        if not terms:
            errors.append('You must agree to share live location with pack members.')

        if errors:
            return render(request, 'register.html', {
                'errors': errors,
                'form_data': {
                    'fullname': fullname,
                    'email': email,
                    'phone': phone,
                    'vehicle': ride_type,
                    'ride_type': ride_type,
                }
            })

        try:
            with transaction.atomic():
                username = email
                user = User.objects.create_user(
                    username=username,
                    email=email,
                    password=password,
                    first_name=fullname
                )

                profile, _ = RiderProfile.objects.get_or_create(user=user)
                profile.role = 'rider'
                profile.phone = phone
                profile.ride_type = ride_type
                profile.location_consent = bool(terms)
                profile.save()

                user.backend = 'tracker.backends.EmailAuthBackend'
                auth_login(request, user)
                return redirect('dashboard')
        except Exception as e:
            return render(request, 'register.html', {
                'errors': [f'Registration failed: {str(e)}'],
                'form_data': {
                    'fullname': fullname,
                    'email': email,
                    'phone': phone,
                    'vehicle': ride_type,
                    'ride_type': ride_type,
                }
            })

    return render(request, 'register.html')


def login_view(request):
    """
    Authenticates against Django database using secure hashed password verification.
    """
    if request.user.is_authenticated:
        return redirect('dashboard')

    error_message = None
    next_url = request.GET.get('next', 'dashboard')

    if request.method == 'POST':
        email = request.POST.get('email', '').strip().lower()
        password = request.POST.get('password', '')
        remember = request.POST.get('remember')

        user = authenticate(request, username=email, password=password)

        if user is not None and user.is_active:
            auth_login(request, user)
            if not remember:
                request.session.set_expiry(0)
            else:
                request.session.set_expiry(1209600)
            return redirect(next_url or 'dashboard')
        else:
            error_message = 'Invalid email or password. Please try again.'

    return render(request, 'login.html', {
        'error_message': error_message,
        'email_val': request.POST.get('email', '') if request.method == 'POST' else ''
    })


def logout_view(request):
    """
    Destroys the authenticated session and marks rider offline.
    """
    if request.user.is_authenticated and hasattr(request.user, 'profile'):
        request.user.profile.is_online = False
        request.user.profile.save(update_fields=['is_online'])
        # Also mark active memberships disconnected, preserving the ride membership state.
        RideMember.objects.filter(user=request.user, status='ACTIVE').update(
            status='DISCONNECTED',
            is_connected=False,
            last_seen=timezone.now()
        )
    auth_logout(request)
    return redirect('login')


# ==========================================
# RIDER VIEWS & FUNCTIONALITY
# ==========================================

@login_required(login_url='login')
def dashboard_view(request, pack_id=None):
    """
    Main Rider & Admin live tracking dashboard view.
    """
    user = request.user
    profile, _ = RiderProfile.objects.get_or_create(user=user)
    profile.is_online = True
    profile.save(update_fields=['is_online', 'last_active'])

    packs = RideGroup.objects.filter(
        status__in=['READY', 'ACTIVE'],
    ).filter(
        Q(created_by=user) | Q(members__user=user),
    ).distinct().order_by('-created_at')
    active_pack = None
    if pack_id:
        selected_pack = get_object_or_404(RideGroup, id=pack_id)
        if can_view_group(user, selected_pack) and selected_pack.status in ['READY', 'ACTIVE']:
            active_pack = selected_pack
        else:
            active_pack = resolve_dashboard_group_for_user(user, requested_pack_id=None, groups=packs)
    else:
        active_pack = resolve_dashboard_group_for_user(user, requested_pack_id=None, groups=packs)

    members = []
    active_alerts = []
    active_ride = active_pack if active_pack and active_pack.status == 'ACTIVE' else None
    pack_status = active_pack.status if active_pack else None
    is_active_pack_member = bool(
        active_pack and RideMember.objects.filter(
            group=active_pack, user=user, status='ACTIVE',
        ).exists()
    )
    if active_pack:
        # Update user's member presence in the selected group before the ride starts or while live.
        member_obj = RideMember.objects.filter(group=active_pack, user=user).first()
        if member_obj:
            member_obj.status = 'ACTIVE'
            member_obj.is_connected = True
            member_obj.last_seen = timezone.now()
            member_obj.save(update_fields=['status', 'is_connected', 'last_seen'])

        from .services import get_rider_color
        memberships = active_pack.members.filter(status__in=['ACTIVE', 'DISCONNECTED']).select_related('user', 'user__profile').order_by('rider_number', 'id')
        for m in memberships.order_by('rider_number', 'id'):
            m_user = m.user
            m_profile = getattr(m_user, 'profile', None)
            latest_loc = m.location_logs.first()
            last_speed = round(latest_loc.speed, 1) if latest_loc and m.is_connected else None
            last_lat = latest_loc.latitude if latest_loc else None
            last_lng = latest_loc.longitude if latest_loc else None
            members.append({
                'id': f"r{m_user.id}",
                'user_id': m_user.id,
                'rider_number': m.rider_number,
                'name': m_user.get_full_name() or m_user.first_name or m_user.username,
                'role': m.get_role_display(),
                'vehicle': m_profile.ride_type if m_profile else 'Motorcycle',
                'initials': m_profile.avatar_initials if (m_profile and m_profile.avatar_initials) else m_user.username[:2].upper(),
                'color': get_rider_color(m.rider_number),
                'is_online': m.is_connected,
                'is_connected': m.is_connected,
                'status': m.status,
                'last_seen': m.last_seen,
                'is_current_user': (m_user == user),
                'speed': last_speed,
                'latitude': last_lat,
                'longitude': last_lng,
            })
        if active_ride:
            active_alerts = list(active_ride.sos_alerts.filter(status='ACTIVE').values(
                'id', 'message', 'latitude', 'longitude', 'timestamp',
                'ride_member__user__first_name', 'ride_member__user__username', 'ride_member__user__id',
                'ride_member__rider_number'
            ))
            for alert in active_alerts:
                alert['rider_color'] = get_rider_color(alert['ride_member__rider_number'])

    online_riders_count = sum(1 for member in members if member['is_online'])
    speeds = [member['speed'] for member in members if member['is_online'] and member['speed'] is not None]
    avg_speed_kmh = round(sum(speeds) / len(speeds), 1) if speeds else 0
    elapsed_seconds = 0
    if active_ride and active_ride.started_at:
        elapsed_seconds = max(0, int((timezone.now() - active_ride.started_at).total_seconds()))
    elapsed_display = f'{elapsed_seconds // 60}m {elapsed_seconds % 60:02d}s'

    context = {
        'user': user,
        'profile': profile,
        'is_admin': profile.is_admin,
        'packs': packs,
        'active_pack': active_pack,
        'members': members,
        'active_alerts': active_alerts,
        'online_riders_count': online_riders_count,
        'total_riders_count': len(members),
        'avg_speed_kmh': avg_speed_kmh,
        'elapsed_display': elapsed_display,
        'google_maps_api_key': settings.GOOGLE_MAPS_API_KEY,
        'is_active_pack_member': is_active_pack_member,
    }
    return render(request, 'dashboard.html', context)


@login_required(login_url='login')
@require_POST
def create_group_view(request):
    """
    Riders can create new riding packs/groups with unique join codes.
    """
    name = request.POST.get('name', '').strip()
    destination = request.POST.get('destination_name', '').strip()
    description = request.POST.get('description', '').strip()
    latitude_text = request.POST.get('destination_latitude', '').strip()
    longitude_text = request.POST.get('destination_longitude', '').strip()
    start_latitude_text = request.POST.get('start_latitude', '').strip()
    start_longitude_text = request.POST.get('start_longitude', '').strip()
    route_distance_text = request.POST.get('route_distance_meters', '').strip()
    route_duration_text = request.POST.get('route_duration_seconds', '').strip()
    planned_route = parse_planned_route(request.POST.get('planned_route', ''))

    if not name:
        return JsonResponse({'error': 'Group name is required'}, status=400)
    if not destination:
        return JsonResponse({'error': 'Please select a destination.'}, status=400)
    if len(destination) > 150:
        return JsonResponse({'error': 'Destination must be 150 characters or fewer.'}, status=400)

    try:
        destination_latitude = Decimal(latitude_text)
        destination_longitude = Decimal(longitude_text)
        start_latitude = float(start_latitude_text)
        start_longitude = float(start_longitude_text)
        route_distance_meters = float(route_distance_text)
        route_duration_value = float(route_duration_text)
        if (not destination_latitude.is_finite() or not destination_longitude.is_finite()
                or not is_valid_coordinate_pair(float(destination_latitude), float(destination_longitude))
                or not is_valid_coordinate_pair(start_latitude, start_longitude)
                or not math.isfinite(route_distance_meters) or route_distance_meters <= 0
            or not math.isfinite(route_duration_value) or route_duration_value <= 0 or not planned_route):
            raise ValueError
        route_duration_seconds = math.ceil(route_duration_value)
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        return JsonResponse({'error': 'Get your current location, select a destination, and calculate a valid route before creating the ride.'}, status=400)

    group = RideGroup.objects.create(
        name=name,
        description=description,
        destination_name=destination,
        destination_latitude=destination_latitude,
        destination_longitude=destination_longitude,
        start_latitude=start_latitude,
        start_longitude=start_longitude,
        planned_route=planned_route,
        route_distance_meters=route_distance_meters,
        route_duration_seconds=route_duration_seconds,
        created_by=request.user,
        status='READY'
    )
    # Creator becomes Leader
    RideMember.objects.create(group=group, user=request.user, role='LEADER', status='ACTIVE', is_connected=True)

    return JsonResponse({
        'success': True,
        'group_id': group.id,
        'join_code': group.join_code,
        'group_name': group.name,
        'redirect_url': f'/dashboard/{group.id}/'
    })


@require_POST
def join_group_api_view(request):
    """
    POST /api/groups/join/
    Request:
    {
        "join_code": "PT7K4Q"
    }

    1. Verify authenticated user.
    2. Verify group exists.
    3. Verify group is active/joinable.
    4. Check whether user is already a member.
    5. Create RideMember record.
    6. Return group information.
    7. Return current group members.
    """
    # 1. Verify authenticated user
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'Authentication required. Please log in first.'}, status=401)

    # Parse payload (JSON or Form Data)
    join_code = None
    if request.body:
        try:
            body_data = json.loads(request.body.decode('utf-8'))
            join_code = body_data.get('join_code')
        except Exception:
            pass
    if not join_code:
        join_code = request.POST.get('join_code')

    if not join_code or not str(join_code).strip():
        return JsonResponse({'error': 'Join code is required.'}, status=400)

    join_code = str(join_code).strip().upper()

    # 2. Verify group exists
    group = RideGroup.objects.filter(join_code__iexact=join_code).first()
    if not group:
        return JsonResponse({'error': 'Invalid join code. Group not found.'}, status=404)

    # 3. Verify group is active/joinable
    if group.status == 'ENDED':
        return JsonResponse({'error': 'This ride has already ended and cannot be joined.'}, status=400)
    if group.status not in ['READY', 'CREATED', 'ACTIVE', 'PAUSED']:
        return JsonResponse({'error': f'Cannot join group in {group.get_status_display()} state.'}, status=400)


    # 4. Check whether user is already a member
    member = RideMember.objects.filter(group=group, user=request.user).first()
    if member and member.status == 'ACTIVE':
        return JsonResponse({
            'error': 'You are already an active member of this group.',
            'already_member': True,
            'group_id': group.id,
            'redirect_url': f'/dashboard/{group.id}/'
        }, status=400)

    # 5. Create or re-activate RideMember record
    if member:
        member.status = 'ACTIVE'
        member.is_connected = True
        member.last_seen = timezone.now()
        member.save(update_fields=['status', 'is_connected', 'last_seen'])
    else:
        member = RideMember.objects.create(
            group=group,
            user=request.user,
            role='RIDER',
            status='ACTIVE',
            is_connected=True,
            joined_at=timezone.now()
        )

    # 6 & 7. Return group information & current members
    active_memberships = group.members.filter(status='ACTIVE').select_related('user', 'user__profile').order_by('rider_number', 'id')
    members_list = []
    from .services import get_rider_color
    for m in active_memberships:
        u = m.user
        p = getattr(u, 'profile', None)
        members_list.append({
            'id': f"r{u.id}",
            'user_id': u.id,
            'rider_number': m.rider_number,
            'name': u.get_full_name() or u.first_name or u.username,
            'role': m.get_role_display(),
            'status': m.status,
            'vehicle': p.ride_type if p else 'Motorcycle',
            'initials': p.avatar_initials if (p and p.avatar_initials) else u.username[:2].upper(),
            'color': get_rider_color(m.rider_number),
            'is_connected': m.is_connected,
            'is_current_user': (u == request.user),
        })

    return JsonResponse({
        'success': True,
        'message': f'Successfully joined {group.name}',
        'group': {
            'id': group.id,
            'name': group.name,
            'join_code': group.join_code,
            'status': group.get_status_display(),
            'destination': group.destination_name,
            'created_by': group.created_by.get_full_name() or group.created_by.username,
            'total_riders': len(members_list),
            'started_at': group.started_at.isoformat() if group.started_at else None,
        },
        'members': members_list,
        'redirect_url': f'/dashboard/{group.id}/'
    })


@login_required(login_url='login')
@require_POST
def join_group_view(request, pack_id=None):
    """
    Riders can join active groups via group ID or join_code.
    """
    return join_group_api_view(request)



def is_group_leader(user, group):
    """
    Checks if a user is the creator or has the active LEADER role in the group.
    """
    if not user.is_authenticated:
        return False
    if group.created_by == user or user.is_staff or user.is_superuser:
        return True
    membership = RideMember.objects.filter(group=group, user=user, status='ACTIVE').first()
    return membership is not None and membership.role == 'LEADER'


def can_view_group(user, group):
    return (
        user.is_authenticated
        and (user.is_staff or user.is_superuser or group.created_by_id == user.id
             or RideMember.objects.filter(group=group, user=user).exists())
    )


@login_required(login_url='login')
def list_user_groups_api(request):
    """
    GET /api/groups/
    Lists all groups the authenticated user is a member of or created.
    """
    user = request.user
    memberships = RideMember.objects.filter(user=user).select_related('group', 'group__created_by')
    groups_list = []
    for m in memberships:
        g = m.group
        groups_list.append({
            'id': g.id,
            'name': g.name,
            'join_code': g.join_code,
            'destination': g.destination_name,
            'status': g.status,
            'status_display': g.get_status_display(),
            'your_role': m.role,
            'your_role_display': m.get_role_display(),
            'is_leader': (m.role == 'LEADER' or g.created_by == user),
            'total_riders': g.members.filter(status='ACTIVE').count(),
            'created_at': g.created_at.isoformat() if g.created_at else None,
            'started_at': g.started_at.isoformat() if g.started_at else None,
            'ended_at': g.ended_at.isoformat() if g.ended_at else None,
        })
    return JsonResponse({'success': True, 'groups': groups_list})


@login_required(login_url='login')
def view_group_api(request, group_id):
    """
    GET /api/groups/<int:group_id>/
    Returns full details for a specific group.
    """
    group = get_object_or_404(RideGroup, id=group_id)
    if not can_view_group(request.user, group):
        return JsonResponse({'error': 'Permission denied.'}, status=403)
    membership = RideMember.objects.filter(group=group, user=request.user, status='ACTIVE').first()
    active_members_count = group.members.filter(status='ACTIVE').count()

    return JsonResponse({
        'success': True,
        'group': {
            'id': group.id,
            'name': group.name,
            'join_code': group.join_code,
            'description': group.description,
            'destination': group.destination_name,
            'next_waypoint': group.destination_name if group.status == 'ACTIVE' and group.destination_name else '',
            'status': group.status,
            'status_display': group.get_status_display(),
            'created_by': {
                'id': group.created_by.id,
                'name': group.created_by.get_full_name() or group.created_by.username,
                'email': group.created_by.email
            },
            'total_riders': active_members_count,
            'created_at': group.created_at.isoformat() if group.created_at else None,
            'started_at': group.started_at.isoformat() if group.started_at else None,
            'ended_at': group.ended_at.isoformat() if group.ended_at else None,
            'is_leader': is_group_leader(request.user, group),
            'is_member': (membership is not None),
            'user_role': membership.role if membership else None
        }
    })


@login_required(login_url='login')
def view_group_members_api(request, group_id):
    """
    GET /api/groups/<int:group_id>/members/
    Returns all members belonging to the group.
    """
    group = get_object_or_404(RideGroup, id=group_id)
    if not can_view_group(request.user, group):
        return JsonResponse({'error': 'Permission denied.'}, status=403)
    memberships = group.members.select_related('user', 'user__profile').order_by('rider_number', 'id')
    members_list = []
    from .services import get_rider_color
    for m in memberships:
        u = m.user
        p = getattr(u, 'profile', None)
        members_list.append({
            'id': f"r{u.id}",
            'member_id': m.id,
            'user_id': u.id,
            'rider_number': m.rider_number,
            'name': u.get_full_name() or u.first_name or u.username,
            'role': m.role,
            'role_display': m.get_role_display(),
            'status': m.status,
            'status_display': m.get_status_display(),
            'is_connected': m.is_connected,
            'last_seen': m.last_seen.isoformat() if m.last_seen else None,
            'joined_at': m.joined_at.isoformat() if m.joined_at else None,
            'vehicle': p.ride_type if p else 'Motorcycle',
            'initials': p.avatar_initials if (p and p.avatar_initials) else u.username[:2].upper(),
            'color': get_rider_color(m.rider_number),
            'is_current_user': (u == request.user),
        })

    return JsonResponse({
        'success': True,
        'group_id': group.id,
        'group_name': group.name,
        'total_members': len(members_list),
        'members': members_list
    })


@login_required(login_url='login')
@require_POST
def start_ride_view(request, pack_id=None, group_id=None):
    """
    POST /api/groups/<int:group_id>/start/ and POST /rides/<int:pack_id>/start/
    When a leader starts a ride:
    - Group status: READY -> ACTIVE
    - Stores started_at and active status
    - Notifies connected group members
    - Switches group to live tracking mode
    """
    target_id = group_id or pack_id
    group = get_object_or_404(RideGroup, id=target_id)

    if not is_group_leader(request.user, group):
        return JsonResponse({
            'error': 'Permission denied: Only the group leader or creator can start the ride.'
        }, status=403)

    if group.status != 'READY':
        return JsonResponse({'error': 'This ride is not ready to start.'}, status=400)

    try:
        payload = json.loads(request.body.decode('utf-8')) if request.body else {}
        start_latitude = float(payload['start_latitude'])
        start_longitude = float(payload['start_longitude'])
        route_distance_meters = float(payload['route_distance_meters'])
        route_duration_value = float(payload['route_duration_seconds'])
        planned_route = parse_planned_route(payload.get('planned_route'))
        if (not is_valid_coordinate_pair(start_latitude, start_longitude)
                or not math.isfinite(route_distance_meters) or route_distance_meters <= 0
            or not math.isfinite(route_duration_value) or route_duration_value <= 0 or not planned_route
                or group.destination_latitude is None or group.destination_longitude is None):
            raise ValueError
        route_duration_seconds = math.ceil(route_duration_value)
        from .services import haversine
        if (haversine(start_latitude, start_longitude, *planned_route[0]) > 500
                or haversine(float(group.destination_latitude), float(group.destination_longitude), *planned_route[-1]) > 500):
            raise ValueError
    except (KeyError, TypeError, ValueError, OverflowError, json.JSONDecodeError):
        return JsonResponse({'error': 'A current GPS fix and a valid calculated route are required to start this ride.'}, status=400)

    now = timezone.now()
    group.status = 'ACTIVE'
    group.started_at = now
    group.start_latitude = start_latitude
    group.start_longitude = start_longitude
    group.planned_route = planned_route
    group.route_distance_meters = route_distance_meters
    group.route_duration_seconds = route_duration_seconds
    group.save(update_fields=[
        'status', 'started_at', 'start_latitude', 'start_longitude',
        'planned_route', 'route_distance_meters', 'route_duration_seconds',
    ])

    # Update all connected members
    group.members.filter(status__in=['INVITED', 'ACTIVE', 'DISCONNECTED']).update(
        status='ACTIVE',
        is_connected=True,
        last_seen=now
    )

    # Broadcast notification to group
    leader_name = request.user.get_full_name() or request.user.first_name or request.user.username

    return JsonResponse({
        'success': True,
        'message': f'Ride for {group.name} started by {leader_name}. Live tracking active.',
        'status': group.status,
        'started_at': group.started_at.isoformat(),
        'is_live': True
    })


@login_required(login_url='login')
def group_route_api(request, group_id):
    """
    GET /api/groups/<int:group_id>/route/
    Returns the planned route waypoints for a ride group.
    Frontend can use this to dynamically render the planned route
    on the dashboard map without a full page reload.

    Response:
    {
        "success": true,
        "group_id": 15,
        "planned_route": [[lat, lng], ...],   // array of [lat, lng] pairs
        "waypoint_count": 13,
        "has_route": true
    }
    """
    group = get_object_or_404(RideGroup, id=group_id)
    if not can_view_group(request.user, group):
        return JsonResponse({'error': 'Permission denied.'}, status=403)
    planned_route = group.planned_route or []

    # Normalise to [[lat, lng]] format regardless of storage format
    normalised = []
    for wp in planned_route:
        if isinstance(wp, (list, tuple)) and len(wp) >= 2:
            try:
                normalised.append([float(wp[0]), float(wp[1])])
            except (TypeError, ValueError):
                pass
        elif isinstance(wp, dict):
            lat = wp.get('lat') or wp.get('latitude')
            lng = wp.get('lng') or wp.get('longitude')
            if lat is not None and lng is not None:
                try:
                    normalised.append([float(lat), float(lng)])
                except (TypeError, ValueError):
                    pass

    return JsonResponse({
        'success': True,
        'group_id': group.id,
        'group_name': group.name,
        'planned_route': normalised,
        'route_distance_meters': group.route_distance_meters,
        'route_duration_seconds': group.route_duration_seconds,
        'waypoint_count': len(normalised),
        'has_route': len(normalised) > 1,
    })


@login_required(login_url='login')
def group_live_state_api(request, group_id):
    """
    GET /api/groups/<int:group_id>/live-state/
    Returns live tracking state, elapsed time, and member coordinates for real-time dashboard sync.
    Runs periodic disconnect detection on stale members.
    """
    from .services import check_and_update_disconnected_riders, handle_rider_reconnect

    group = get_object_or_404(RideGroup, id=group_id)
    if not can_view_group(request.user, group):
        return JsonResponse({'error': 'Permission denied.'}, status=403)

    # Perform disconnect inspection
    check_and_update_disconnected_riders(group)

    # Calculate elapsed time
    elapsed_display = "0m 00s"
    elapsed_seconds = 0
    if group.started_at and group.status == 'ACTIVE':
        diff = timezone.now() - group.started_at
        elapsed_seconds = int(diff.total_seconds())
        hours = elapsed_seconds // 3600
        minutes = (elapsed_seconds % 3600) // 60
        if hours > 0:
            elapsed_display = f"{hours}h {minutes:02d}m"
        else:
            elapsed_display = f"{minutes}m {elapsed_seconds % 60:02d}s"

    memberships = group.members.filter(status__in=['ACTIVE', 'DISCONNECTED']).select_related('user', 'user__profile')
    members_data = []
    from .services import get_rider_color
    total_speed = 0.0
    active_count = 0

    for m in memberships.order_by('rider_number', 'id'):
        u = m.user
        p = getattr(u, 'profile', None)
        latest_loc = m.location_logs.first()
        speed = latest_loc.speed if latest_loc else 0.0
        if m.is_connected:
            total_speed += speed
            active_count += 1

        members_data.append({
            'id': f"r{u.id}",
            'member_id': m.id,
            'user_id': u.id,
            'rider_number': m.rider_number,
            'name': u.get_full_name() or u.first_name or u.username,
            'role': m.role,
            'role_display': m.get_role_display(),
            'status': m.status,
            'status_display': m.get_status_display(),
            'vehicle': p.ride_type if p else 'Motorcycle',
            'initials': p.avatar_initials if (p and p.avatar_initials) else u.username[:2].upper(),
            'color': get_rider_color(m.rider_number),
            'is_connected': m.is_connected,
            'last_seen': m.last_seen.isoformat() if m.last_seen else None,
            'route_deviation': m.route_deviation,
            'latitude': latest_loc.latitude if latest_loc else None,
            'longitude': latest_loc.longitude if latest_loc else None,
            'speed': round(speed, 1) if m.is_connected else 0,
            'heading': latest_loc.heading if latest_loc else 0.0,
            'is_current_user': (u == request.user),
        })

    avg_speed = round(total_speed / active_count, 1) if active_count > 0 else 0.0
    active_alerts = list(group.sos_alerts.filter(status='ACTIVE').values(
        'id', 'message', 'timestamp', 'ride_member__user__first_name', 'ride_member__user_id',
        'ride_member__rider_number'
    ))

    return JsonResponse({
        'success': True,
        'group_id': group.id,
        'group_name': group.name,
        'status': group.status,
        'is_live': (group.status == 'ACTIVE'),
        'started_at': group.started_at.isoformat() if group.started_at else None,
        'elapsed_seconds': elapsed_seconds,
        'elapsed_display': elapsed_display,
        'next_waypoint': group.destination_name if group.status == 'ACTIVE' and group.destination_name else '',
        'avg_speed_kmh': avg_speed,
        'online_riders_count': active_count,
        'total_riders_count': len(members_data),
        'members': members_data,
        'active_alerts': active_alerts,
    })
@require_POST
def update_location_api(request, group_id=None, pack_id=None):
    """
    POST /api/groups/<group_id>/location/ or POST /api/location/update/
    Receives and strictly validates real GPS telemetry from rider:
    - latitude: -90.0 to 90.0
    - longitude: -180.0 to 180.0
    - speed: numeric >= 0
    - accuracy: numeric >= 0 (optional)
    - heading: 0 to 360 (optional)
    Only allows authenticated riders belonging to an ACTIVE ride.
    """
    # 1. Authenticated user check
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'Authentication required.'}, status=401)

    # Parse payload (JSON or Form Data)
    data = {}
    if request.body:
        try:
            data = json.loads(request.body.decode('utf-8'))
        except Exception:
            pass
    if not data:
        data = request.POST.dict()

    target_group_id = group_id or pack_id or data.get('group_id')
    if not target_group_id:
        active_membership = RideMember.objects.filter(
            user=request.user,
            status='ACTIVE',
            group__status='ACTIVE'
        ).first()
        if active_membership:
            group = active_membership.group
        else:
            return JsonResponse({'error': 'No active ride group specified or found for rider.'}, status=400)
    else:
        group = get_object_or_404(RideGroup, id=target_group_id)

    # 2. Check group status is ACTIVE
    if group.status != 'ACTIVE':
        return JsonResponse({'error': f'Cannot send location update: Ride is currently {group.get_status_display()}.'}, status=403)

    # 3. Check active ride membership
    member = RideMember.objects.filter(group=group, user=request.user, status='ACTIVE').first()
    if not member:
        return JsonResponse({'error': 'Permission denied: You must be an active member of this ride.'}, status=403)

    # 4. Strict Coordinate and Telemetry Validation
    try:
        lat_raw = data.get('latitude')
        lng_raw = data.get('longitude')
        if lat_raw is None or lng_raw is None:
            return JsonResponse({'error': 'Both latitude and longitude are required.'}, status=400)

        latitude = float(lat_raw)
        longitude = float(lng_raw)
    except (ValueError, TypeError):
        return JsonResponse({'error': 'Latitude and longitude must be valid numeric values.'}, status=400)

    # Validate coordinate ranges
    if not (-90.0 <= latitude <= 90.0):
        return JsonResponse({'error': f'Latitude {latitude} out of valid range (-90.0 to 90.0).'}, status=400)
    if not (-180.0 <= longitude <= 180.0):
        return JsonResponse({'error': f'Longitude {longitude} out of valid range (-180.0 to 180.0).'}, status=400)

    speed_raw = data.get('speed')

    # Accuracy validation
    accuracy = None
    accuracy_raw = data.get('accuracy')
    if accuracy_raw is not None:
        try:
            accuracy = float(accuracy_raw)
            if accuracy < 0:
                accuracy = None
        except (ValueError, TypeError):
            pass

    # Heading validation
    heading = 0.0
    heading_raw = data.get('heading')
    if heading_raw is not None:
        try:
            heading = float(heading_raw) % 360.0
        except (ValueError, TypeError):
            pass

    now = timezone.now()

    from .services import calculate_or_validate_speed
    final_speed = calculate_or_validate_speed(member, latitude, longitude, speed_raw, now)

    # 5. Store LocationLog in database
    log = LocationLog.objects.create(
        ride_member=member,
        latitude=latitude,
        longitude=longitude,
        speed=final_speed,
        accuracy=accuracy,
        heading=heading,
        timestamp=now
    )

    # Update member connectivity state and handle reconnect if previously disconnected
    from .services import handle_rider_reconnect, check_route_deviation
    handle_rider_reconnect(member)
    
    deviation_payload = check_route_deviation(member, latitude, longitude)
    if deviation_payload:
        from channels.layers import get_channel_layer
        from asgiref.sync import async_to_sync
        channel_layer = get_channel_layer()
        if channel_layer:
            try:
                async_to_sync(channel_layer.group_send)(
                    f"ride_group_{group.id}",
                    {
                        'type': 'broadcast_sos',  # Reuse generic broadcast handler
                        'message': deviation_payload
                    }
                )
            except Exception:
                pass

    from .services import get_rider_color
    profile = getattr(request.user, 'profile', None)
    rider_name = request.user.get_full_name() or request.user.first_name or request.user.username
    location_payload = {
        'type': 'location_update',
        'ride_id': group.id,
        'rider_id': request.user.id,
        'rider_number': member.rider_number,
        'name': rider_name,
        'initials': profile.avatar_initials if profile else request.user.username[:2].upper(),
        'color': get_rider_color(member.rider_number),
        'role': member.get_role_display(),
        'vehicle': profile.ride_type if profile else 'Motorcycle',
        'latitude': latitude,
        'longitude': longitude,
        'speed': round(final_speed, 1),
        'heading': round(heading, 1),
        'accuracy': accuracy,
        'timestamp': now.isoformat(),
    }
    from channels.layers import get_channel_layer
    from asgiref.sync import async_to_sync
    channel_layer = get_channel_layer()
    if channel_layer:
        async_to_sync(channel_layer.group_send)(
            f'ride_group_{group.id}',
            {'type': 'broadcast_location', 'message': location_payload},
        )

    # Update rider profile
    if hasattr(request.user, 'profile'):
        request.user.profile.is_online = True
        request.user.profile.last_active = now
        request.user.profile.save(update_fields=['is_online', 'last_active'])

    return JsonResponse({
        'success': True,
        'message': 'Location logged successfully.',
        'log_id': log.id,
        'latitude': latitude,
        'longitude': longitude,
        'speed': round(final_speed, 1),
        'ride_id': group.id,
        'rider_id': request.user.id,
        'rider_number': member.rider_number,
        'rider_name': rider_name,
        'color': get_rider_color(member.rider_number),
        'accuracy': accuracy,
        'heading': round(heading, 1),
        'timestamp': now.isoformat()
    })




@login_required(login_url='login')
@require_POST
def end_ride_view(request, pack_id=None, group_id=None):
    """
    POST /api/groups/<int:group_id>/end/
    When the leader ends the ride:
    - Group status: ACTIVE -> COMPLETED
    - Store: ended_at
    - Stop live tracking for the group
    - Preserve all location logs in database
    - Ride becomes available in ride history
    """
    target_id = group_id or pack_id
    group = get_object_or_404(RideGroup, id=target_id)

    if not is_group_leader(request.user, group):
        return JsonResponse({
            'error': 'Permission denied: Only the group leader or creator can end the ride.'
        }, status=403)

    now = timezone.now()
    group.status = 'COMPLETED'
    group.ended_at = now
    group.save(update_fields=['status', 'ended_at'])

    # Clear active ride membership state while preserving historical records and logs.
    group.members.filter(status__in=['ACTIVE', 'DISCONNECTED']).update(
        status='ENDED',
        is_connected=False,
        last_seen=now,
        left_at=now,
    )

    return JsonResponse({
        'success': True,
        'message': f'Ride for {group.name} has been completed.',
        'status': group.status,
        'ended_at': group.ended_at.isoformat(),
        'is_live': False,
        'redirect_url': '/rides/history/'
    })



@login_required(login_url='login')
@require_POST
def leave_group_view(request, pack_id=None, group_id=None):
    """
    POST /api/groups/<int:group_id>/leave/ and POST /groups/<int:pack_id>/leave/
    Rider leaves the group.
    """
    target_id = group_id or pack_id
    group = get_object_or_404(RideGroup, id=target_id)
    member = RideMember.objects.filter(group=group, user=request.user).first()
    if member:
        member.status = 'LEFT'
        member.left_at = timezone.now()
        member.is_connected = False
        member.save(update_fields=['status', 'left_at', 'is_connected'])

    return JsonResponse({
        'success': True,
        'message': f'Successfully left {group.name}',
        'group_id': group.id
    })



@login_required(login_url='login')
@require_POST
def send_sos_view(request, pack_id):
    """
    Riders can send an emergency SOS alert with their current coordinates.
    """
    group = get_object_or_404(RideGroup, id=pack_id)
    member = RideMember.objects.filter(group=group, user=request.user, status='ACTIVE').first()
    if group.status != 'ACTIVE' or not member:
        return JsonResponse({'error': 'You must be an active member of this ride to send SOS.'}, status=403)

    message = request.POST.get('message', 'Emergency SOS signal broadcasted by rider!')
    lat_text = request.POST.get('latitude')
    lng_text = request.POST.get('longitude')
    try:
        lat = float(lat_text) if lat_text not in (None, '') else None
        lng = float(lng_text) if lng_text not in (None, '') else None
    except (TypeError, ValueError):
        return JsonResponse({'error': 'SOS coordinates must be valid numbers.'}, status=400)
    if (lat is None) != (lng is None) or (lat is not None and not is_valid_coordinate_pair(lat, lng)):
        return JsonResponse({'error': 'SOS coordinates are outside valid ranges.'}, status=400)

    alert = SOSAlert.objects.create(
        ride_member=member,
        latitude=lat,
        longitude=lng,
        message=message,
        status='ACTIVE'
    )
    from .services import get_rider_color
    rider_name = request.user.get_full_name() or request.user.first_name or request.user.username
    timestamp = alert.timestamp.isoformat()
    payload = {
        'type': 'sos_alert',
        'alert_id': alert.id,
        'ride_id': group.id,
        'rider_id': request.user.id,
        'rider_number': member.rider_number,
        'rider_color': get_rider_color(member.rider_number),
        'rider_name': rider_name,
        'name': rider_name,
        'message': alert.message,
        'latitude': alert.latitude,
        'longitude': alert.longitude,
        'timestamp': timestamp,
    }
    from channels.layers import get_channel_layer
    from asgiref.sync import async_to_sync
    channel_layer = get_channel_layer()
    if channel_layer:
        async_to_sync(channel_layer.group_send)(
            f'ride_group_{group.id}',
            {'type': 'broadcast_sos', 'message': payload},
        )
    return JsonResponse({
        'success': True,
        'alert_id': alert.id,
        'ride_id': group.id,
        'rider_id': request.user.id,
        'rider_number': member.rider_number,
        'rider_name': rider_name,
        'message': alert.message,
        'status': alert.status,
        'timestamp': timestamp,
    })


@login_required(login_url='login')
def ride_history_view(request):
    """
    Riders can view their own and joined rides history.
    """
    user = request.user
    memberships = RideMember.objects.filter(
        user=user,
        group__status__in=['COMPLETED', 'ENDED'],
    ).select_related('group')
    rides_data = []
    for m in memberships:
        g = m.group
        logs = list(m.location_logs.order_by('timestamp').only('latitude', 'longitude', 'timestamp'))
        travelled_meters = 0.0
        if len(logs) > 1:
            from .services import haversine
            travelled_meters = sum(
                haversine(previous.latitude, previous.longitude, current.latitude, current.longitude)
                for previous, current in zip(logs, logs[1:])
            )
        start_latitude = g.start_latitude if g.start_latitude is not None else (logs[0].latitude if logs else None)
        start_longitude = g.start_longitude if g.start_longitude is not None else (logs[0].longitude if logs else None)
        ride_duration_seconds = (
            max(0, int((g.ended_at - g.started_at).total_seconds()))
            if g.started_at and g.ended_at else None
        )
        rides_data.append({
            'id': g.id,
            'name': g.name,
            'join_code': g.join_code,
            'destination': g.destination_name,
            'start_location': (
                f'{start_latitude:.5f}, {start_longitude:.5f}'
                if start_latitude is not None and start_longitude is not None else 'Not recorded'
            ),
            'route_distance_km': round(g.route_distance_meters / 1000, 1) if g.route_distance_meters else None,
            'travelled_distance_km': round(travelled_meters / 1000, 1),
            'duration_minutes': round(ride_duration_seconds / 60) if ride_duration_seconds is not None else None,
            'role': m.get_role_display(),
            'status': g.get_status_display(),
            'started_at': g.started_at.strftime('%Y-%m-%d %H:%M') if g.started_at else None,
            'ended_at': g.ended_at.strftime('%Y-%m-%d %H:%M') if g.ended_at else None,
            'total_riders': g.members.filter(status='ACTIVE').count(),
        })

    if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.path.startswith('/api/'):
        return JsonResponse({'rides': rides_data})

    return render(request, 'rides_history.html', {'rides': rides_data, 'profile': getattr(user, 'profile', None)})


# ==========================================
# ADMIN-ONLY MONITORING & AUTHORIZATION VIEWS
# ==========================================

@admin_required
def admin_monitor_view(request):
    """
    Admin-only monitoring dashboard:
    - Monitor active rides
    - View all riders (online and disconnected)
    - View groups
    - View rider locations
    - View and resolve SOS alerts
    - View ride history
    """
    groups = RideGroup.objects.all().order_by('-created_at')
    active_groups = groups.filter(status='ACTIVE')
    completed_groups = groups.filter(status='ENDED')

    # All riders and connectivity status
    five_min_ago = timezone.now() - timedelta(minutes=5)
    riders = RiderProfile.objects.select_related('user').all()
    online_riders = riders.filter(is_online=True, last_active__gte=five_min_ago)
    disconnected_riders = riders.exclude(id__in=online_riders.values_list('id', flat=True))

    # SOS Alerts
    active_alerts = SOSAlert.objects.filter(status='ACTIVE').select_related('ride_member__user', 'ride_member__group')
    resolved_alerts = SOSAlert.objects.filter(status='RESOLVED').select_related('ride_member__user', 'ride_member__group', 'resolved_by')[:20]

    # Latest location logs
    latest_locations = LocationLog.objects.select_related('ride_member__user', 'ride_member__group')[:30]

    context = {
        'user': request.user,
        'profile': getattr(request.user, 'profile', None),
        'total_packs_count': groups.count(),
        'active_packs': active_groups,
        'completed_packs': completed_groups,
        'total_riders_count': riders.count(),
        'online_riders': online_riders,
        'disconnected_riders': disconnected_riders,
        'active_alerts': active_alerts,
        'resolved_alerts': resolved_alerts,
        'latest_locations': latest_locations,
    }
    return render(request, 'admin_monitor.html', context)


@admin_required
@require_POST
def acknowledge_alert_view(request, alert_id):
    """
    Admin acknowledges an active SOS alert.
    """
    alert = get_object_or_404(SOSAlert, id=alert_id)
    alert.status = 'ACKNOWLEDGED'
    alert.save(update_fields=['status'])

    from channels.layers import get_channel_layer
    from asgiref.sync import async_to_sync
    channel_layer = get_channel_layer()
    if channel_layer:
        try:
            async_to_sync(channel_layer.group_send)(
                f"ride_group_{alert.ride_member.group_id}",
                {
                    'type': 'broadcast_sos',
                    'message': {
                        'type': 'sos_acknowledged',
                        'alert_id': alert.id,
                        'rider_id': alert.ride_member.user.id,
                        'message': 'SOS has been Acknowledged by Admin'
                    }
                }
            )
        except Exception:
            pass

    return JsonResponse({
        'success': True,
        'message': f'SOS Alert #{alert.id} marked as acknowledged.',
        'alert_id': alert.id
    })


@admin_required
@require_POST
def resolve_alert_view(request, alert_id):
    """
    Admin resolves an active SOS alert.
    """
    alert = get_object_or_404(SOSAlert, id=alert_id)
    alert.status = 'RESOLVED'
    alert.resolved_at = timezone.now()
    alert.resolved_by = request.user
    alert.save(update_fields=['status', 'resolved_at', 'resolved_by'])

    from channels.layers import get_channel_layer
    from asgiref.sync import async_to_sync
    channel_layer = get_channel_layer()
    if channel_layer:
        try:
            async_to_sync(channel_layer.group_send)(
                f"ride_group_{alert.ride_member.group_id}",
                {
                    'type': 'broadcast_sos',
                    'message': {
                        'type': 'sos_resolved',
                        'alert_id': alert.id,
                        'rider_id': alert.ride_member.user.id,
                        'resolved_by': request.user.get_full_name() or request.user.username,
                        'message': 'SOS has been Resolved'
                    }
                }
            )
        except Exception:
            pass

    return JsonResponse({
        'success': True,
        'message': f'SOS Alert #{alert.id} marked as resolved.',
        'alert_id': alert.id
    })


@admin_required
def admin_overview_api(request):
    """
    Admin JSON telemetry overview endpoint.
    """
    five_min_ago = timezone.now() - timedelta(minutes=5)
    active_groups = RideGroup.objects.filter(status='ACTIVE').count()
    active_alerts = SOSAlert.objects.filter(status='ACTIVE').count()
    online_riders = RiderProfile.objects.filter(is_online=True, last_active__gte=five_min_ago).count()
    disconnected_riders = RiderProfile.objects.count() - online_riders

    return JsonResponse({
        'active_packs_count': active_groups,
        'active_alerts_count': active_alerts,
        'online_riders_count': online_riders,
        'disconnected_riders_count': disconnected_riders,
    })
