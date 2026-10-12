import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.utils import timezone
from django.contrib.auth.models import User
from .models import RideGroup, RideMember, LocationLog, SOSAlert, RiderProfile

class RideTrackingConsumer(AsyncWebsocketConsumer):
    """
    Real-time WebSocket consumer for live GPS tracking and pack telemetry.
    Path: /ws/groups/<group_id>/
    """
    async def connect(self):
        self.group_id = self.scope['url_route']['kwargs']['group_id']
        self.room_group_name = f"ride_group_{self.group_id}"
        self.user = self.scope.get('user')
        print(f"[PackTrace WS CONNECT ATTEMPT] user={getattr(self.user, 'id', None)} ride={self.group_id} group={self.room_group_name}")

        # 1. Authenticate user
        if not self.user or not self.user.is_authenticated:
            print(f"[PackTrace WS CONNECT REJECTED] user={getattr(self.user, 'id', None)} ride={self.group_id} reason=unauthenticated")
            await self.close(code=4401)
            return

        # 2. Verify membership / authorization
        membership_info = await self.verify_and_connect_member(self.group_id, self.user)
        if not membership_info['allowed']:
            await self.close(code=4403)
            return


        self.member_id = membership_info.get('member_id')
        self.rider_name = membership_info.get('name')
        self.rider_number = membership_info.get('rider_number')
        self.rider_role = membership_info.get('role')
        self.rider_color = membership_info.get('color')
        self.rider_vehicle = membership_info.get('vehicle')

        # 3. Join Channel Group
        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )
        print(f"[PackTrace WS CONNECT ACCEPTED] user={self.user.id} ride={self.group_id} group={self.room_group_name}")
        await self.accept()

        # 4. Broadcast connection event
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'broadcast_rider_status',
                'message': {
                    'type': 'rider_status_update',
                    'rider_id': self.user.id,
                    'name': self.rider_name,
                    'rider_number': self.rider_number,
                    'role': self.rider_role,
                    'color': self.rider_color,
                    'vehicle': self.rider_vehicle,
                    'latitude': membership_info.get('latitude'),
                    'longitude': membership_info.get('longitude'),
                    'speed': membership_info.get('speed'),
                    'is_connected': True,
                    'timestamp': timezone.now().isoformat()
                }
            }
        )

    async def disconnect(self, close_code):
        if hasattr(self, 'room_group_name'):
            # Mark rider disconnected in DB
            if self.user and self.user.is_authenticated:
                await self.mark_member_disconnected(self.group_id, self.user)

                # Broadcast disconnection to other riders
                await self.channel_layer.group_send(
                    self.room_group_name,
                    {
                        'type': 'broadcast_rider_status',
                        'message': {
                            'type': 'rider_status_update',
                            'rider_id': self.user.id,
                            'name': getattr(self, 'rider_name', self.user.username),
                            'rider_number': getattr(self, 'rider_number', None),
                            'role': getattr(self, 'rider_role', None),
                            'color': getattr(self, 'rider_color', None),
                            'vehicle': getattr(self, 'rider_vehicle', None),
                            'is_connected': False,
                            'timestamp': timezone.now().isoformat()
                        }
                    }
                )

            await self.channel_layer.group_discard(
                self.room_group_name,
                self.channel_name
            )

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
        except Exception:
            await self.send(text_data=json.dumps({'error': 'Invalid JSON format.'}))
            return

        print(f"[PackTrace GPS MESSAGE RECEIVED] user={self.user.id} ride={self.group_id} type={data.get('type', 'location_update')}")
        msg_type = data.get('type', 'location_update')

        # Handle Location Update
        if msg_type in ['location_update', 'location', 'gps_update']:
            await self.handle_location_update(data)
        elif msg_type == 'sos_alert':
            await self.handle_sos_alert(data)
        elif msg_type == 'ping':
            await self.send(text_data=json.dumps({'type': 'pong', 'timestamp': timezone.now().isoformat()}))

    async def handle_location_update(self, data):
        lat_raw = data.get('latitude')
        lng_raw = data.get('longitude')

        if lat_raw is None or lng_raw is None:
            await self.send(text_data=json.dumps({'error': 'Latitude and longitude are required.'}))
            return

        try:
            latitude = float(lat_raw)
            longitude = float(lng_raw)
        except (ValueError, TypeError):
            await self.send(text_data=json.dumps({'error': 'Coordinates must be valid numbers.'}))
            return

        # Coordinate range checks
        if not (-90.0 <= latitude <= 90.0) or not (-180.0 <= longitude <= 180.0):
            await self.send(text_data=json.dumps({'error': 'Coordinates out of valid geographical range.'}))
            return

        speed_raw = data.get('speed')

        try:
            accuracy = float(data.get('accuracy')) if data.get('accuracy') is not None else None
        except (ValueError, TypeError):
            accuracy = None

        try:
            heading = float(data.get('heading', 0.0)) % 360.0
        except (ValueError, TypeError):
            heading = 0.0

        now = timezone.now()

        # Save to LocationLog and update RideMember in database
        save_result = await self.save_telemetry_log(
            group_id=self.group_id,
            user=self.user,
            lat=latitude,
            lng=longitude,
            provided_speed=speed_raw,
            accuracy=accuracy,
            heading=heading,
            now=now
        )

        if not save_result['success']:
            await self.send(text_data=json.dumps({'error': save_result.get('error', 'Failed to save location.')}))
            return

        # Broadcast real-time location update to all connected riders & admins in the group channel
        broadcast_payload = {
            'type': 'location_update',
            'ride_id': int(self.group_id),
            'rider_id': self.user.id,
            'rider_number': save_result['rider_number'],
            'name': save_result['name'],
            'initials': save_result['initials'],
            'color': save_result['color'],
            'role': save_result['role'],
            'vehicle': save_result['vehicle'],
            'latitude': latitude,
            'longitude': longitude,
            'speed': round(save_result['speed'], 1),
            'heading': round(heading, 1),
            'accuracy': accuracy,
            'timestamp': now.isoformat()
        }

        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'broadcast_location',
                'message': broadcast_payload
            }
        )

        # Broadcast route deviation if detected
        deviation_payload = save_result.get('deviation_payload')
        if deviation_payload:
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'broadcast_sos',  # We can reuse this generic broadcaster
                    'message': deviation_payload
                }
            )

    async def handle_sos_alert(self, data):
        message = data.get('message', 'Emergency SOS signal!')
        if not isinstance(message, str):
            await self.send(text_data=json.dumps({'type': 'sos_error', 'message': 'SOS message must be text.'}))
            return
        message = message.strip()
        if not message:
            await self.send(text_data=json.dumps({'type': 'sos_error', 'message': 'Enter an SOS message before sending.'}))
            return
        lat_raw = data.get('latitude')
        lng_raw = data.get('longitude')
        try:
            lat = float(lat_raw) if lat_raw is not None else None
            lng = float(lng_raw) if lng_raw is not None else None
        except (TypeError, ValueError):
            await self.send(text_data=json.dumps({'error': 'SOS coordinates must be valid numbers.'}))
            return
        if (lat is None) != (lng is None) or (
            lat is not None and (not -90 <= lat <= 90 or not -180 <= lng <= 180)
        ):
            await self.send(text_data=json.dumps({'error': 'SOS coordinates are outside valid ranges.'}))
            return
        now = timezone.now()

        alert_info = await self.create_sos_record(self.group_id, self.user, message, lat, lng, now)
        if alert_info['success']:
            rider_name = alert_info['name']
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'broadcast_sos',
                    'message': {
                        'type': 'sos_alert',
                        'alert_id': alert_info['alert_id'],
                        'ride_id': int(self.group_id),
                        'rider_id': self.user.id,
                        'rider_number': alert_info['rider_number'],
                        'rider_color': alert_info['color'],
                        'rider_name': rider_name,
                        'name': rider_name,
                        'message': message,
                        'latitude': lat,
                        'longitude': lng,
                        'timestamp': now.isoformat()
                    }
                }
            )
        else:
            await self.send(text_data=json.dumps({
                'type': 'sos_error',
                'message': 'You must be an active member of this live ride to send SOS.'
            }))

    # Channel Layer Event Handlers
    async def broadcast_location(self, event):
        await self.send(text_data=json.dumps(event['message']))

    async def broadcast_rider_status(self, event):
        await self.send(text_data=json.dumps(event['message']))

    async def broadcast_sos(self, event):
        await self.send(text_data=json.dumps(event['message']))

    # Database Helpers
    @database_sync_to_async
    def verify_and_connect_member(self, group_id, user):
        try:
            group = RideGroup.objects.get(id=group_id)
        except RideGroup.DoesNotExist:
            return {'allowed': False, 'error': 'Group not found.'}

        # Allow admins or staff
        is_admin = (user.is_staff or user.is_superuser or getattr(user.profile, 'role', '') == 'admin')
        member = RideMember.objects.filter(group=group, user=user).first()

        if not member and not is_admin:
            return {'allowed': False, 'error': 'Not a member of this ride.'}

        if member:
            member.status = 'ACTIVE'
            member.is_connected = True
            member.last_seen = timezone.now()
            member.save(update_fields=['status', 'is_connected', 'last_seen'])

        profile = getattr(user, 'profile', None)
        from .services import get_rider_color
        latest_location = member.location_logs.first() if member else None
        return {
            'allowed': True,
            'member_id': member.id if member else None,
            'rider_number': member.rider_number if member else None,
            'name': user.get_full_name() or user.first_name or user.username,
            'initials': profile.avatar_initials if profile else user.username[:2].upper(),
            'color': get_rider_color(member.rider_number) if member else '#3ECF8E',
            'role': member.get_role_display() if member else 'Admin',
            'vehicle': profile.ride_type if profile else 'Motorcycle',
            'latitude': latest_location.latitude if latest_location else None,
            'longitude': latest_location.longitude if latest_location else None,
            'speed': round(latest_location.speed, 1) if latest_location else None,
        }

    @database_sync_to_async
    def mark_member_disconnected(self, group_id, user):
        member = RideMember.objects.filter(group_id=group_id, user=user).first()
        if member:
            member.status = 'DISCONNECTED'
            member.is_connected = False
            member.last_seen = timezone.now()
            member.save(update_fields=['status', 'is_connected', 'last_seen'])

    @database_sync_to_async
    def save_telemetry_log(self, group_id, user, lat, lng, provided_speed, accuracy, heading, now):
        try:
            group = RideGroup.objects.get(id=group_id)
        except RideGroup.DoesNotExist:
            return {'success': False, 'error': 'Group not found.'}

        # Only allow members belonging to ACTIVE rides
        if group.status != 'ACTIVE':
            return {'success': False, 'error': f'Cannot log location: Ride status is {group.status}.'}

        member = RideMember.objects.filter(group=group, user=user, status='ACTIVE').first()
        if not member:
            return {'success': False, 'error': 'User is not an active member of this ride.'}

        from .services import calculate_or_validate_speed
        final_speed = calculate_or_validate_speed(member, lat, lng, provided_speed, now)

        # Save LocationLog
        LocationLog.objects.create(
            ride_member=member,
            latitude=lat,
            longitude=lng,
            speed=final_speed,
            accuracy=accuracy,
            heading=heading,
            timestamp=now
        )

        # Update last_seen, is_connected, and status to ACTIVE
        from .services import handle_rider_reconnect, check_route_deviation
        handle_rider_reconnect(member)

        # Check for route deviation
        deviation_payload = check_route_deviation(member, lat, lng)

        profile = getattr(user, 'profile', None)
        from .services import get_rider_color

        return {
            'success': True,
            'name': user.get_full_name() or user.username,
            'initials': profile.avatar_initials if profile else user.username[:2].upper(),
            'color': get_rider_color(member.rider_number),
            'rider_number': member.rider_number,
            'role': member.get_role_display(),
            'vehicle': profile.ride_type if profile else 'Motorcycle',
            'speed': final_speed,
            'deviation_payload': deviation_payload
        }

    @database_sync_to_async
    def create_sos_record(self, group_id, user, message, lat, lng, now):
        member = RideMember.objects.filter(
            group_id=group_id,
            user=user,
            status='ACTIVE',
            group__status='ACTIVE',
        ).first()
        if not member:
            return {'success': False}

        alert = SOSAlert.objects.create(
            ride_member=member,
            latitude=lat,
            longitude=lng,
            message=message,
            timestamp=now,
            status='ACTIVE'
        )
        from .services import get_rider_color
        return {
            'success': True,
            'alert_id': alert.id,
            'name': user.get_full_name() or user.first_name or user.username,
            'rider_number': member.rider_number,
            'color': get_rider_color(member.rider_number),
        }
