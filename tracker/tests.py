import json
import asyncio
from datetime import timedelta
from unittest.mock import patch
from urllib.error import URLError
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from channels.testing import WebsocketCommunicator
from django.test import TestCase, TransactionTestCase
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from django.conf import settings
from tracker.models import RiderProfile, RideGroup, RideMember, LocationLog, SOSAlert
from tracker.consumers import RideTrackingConsumer
from tracker.services import check_and_update_disconnected_riders, handle_rider_reconnect, DISCONNECT_THRESHOLD
from tracker.routing_service import RouteServiceError, calculate_osrm_route
from tracker.views import resolve_dashboard_group_for_user


class RideDestinationTests(TestCase):
    def setUp(self):
        self.rider = User.objects.create_user(
            username='destination-rider',
            password='Password123!',
        )
        self.client.force_login(self.rider)

    def test_new_profile_is_not_marked_online_without_a_session(self):
        self.assertFalse(self.rider.profile.is_online)

    def test_empty_dashboard_and_history_have_no_demo_application_data(self):
        dashboard = self.client.get('/dashboard/')
        self.assertContains(dashboard, '0 / 0')
        self.assertContains(dashboard, '0 km/h')
        self.assertContains(dashboard, '0m 00s')
        self.assertContains(dashboard, 'No active destination')
        self.assertContains(dashboard, 'No riders currently active')
        self.assertContains(dashboard, 'No rides yet')
        dashboard_html = dashboard.content.decode()
        self.assertEqual(dashboard_html.count('id="gps-status"'), 1)
        self.assertLess(dashboard_html.index('id="gps-status"'), dashboard_html.index('class="map-area"'))
        self.assertContains(dashboard, 'CURRENT LOCATION')

        history = self.client.get('/rides/history/')
        self.assertContains(history, 'No completed rides yet.')

    def test_google_maps_loader_and_create_group_reverse_are_configured(self):
        with self.settings(GOOGLE_MAPS_API_KEY='test-key-not-secret'):
            response = self.client.get('/dashboard/')
        self.assertContains(response, 'maps.googleapis.com/maps/api/js')
        self.assertNotContains(response, 'unpkg.com/leaflet')
        self.assertNotContains(response, 'cartocdn.com')
        self.assertNotContains(response, 'tile.openstreetmap.org')
        self.assertNotContains(response, 'libraries=places')
        self.assertEqual(reverse('create_group'), '/groups/create/')
        with self.settings(GOOGLE_MAPS_API_KEY=''):
            response = self.client.get('/dashboard/')
        self.assertNotContains(response, 'maps.googleapis.com/maps/api/js')
        self.assertContains(response, 'Set GOOGLE_MAPS_API_KEY')

    def test_dashboard_uses_backend_route_endpoint_not_google_directions(self):
        response = self.client.get('/dashboard/')
        self.assertContains(response, "const ROUTE_API_URL = '/api/route/';")
        self.assertNotContains(response, 'DirectionsService')

    def test_dashboard_has_current_location_control(self):
        response = self.client.get('/dashboard/')
        self.assertContains(response, 'Current Location')
        self.assertNotContains(response, 'My Location')

    def test_group_location_apis_require_group_access(self):
        owner = User.objects.create_user(username='map-owner', password='Password123!')
        group = RideGroup.objects.create(name='Private ride', created_by=owner)
        response = self.client.get(f'/api/groups/{group.id}/live-state/')
        self.assertEqual(response.status_code, 403)
        response = self.client.get(f'/api/groups/{group.id}/members/')
        self.assertEqual(response.status_code, 403)

    def test_history_excludes_incomplete_rides(self):
        incomplete = RideGroup.objects.create(
            name='Current ride', created_by=self.rider, status='ACTIVE',
        )
        completed = RideGroup.objects.create(
            name='Past ride', created_by=self.rider, status='COMPLETED',
        )
        RideMember.objects.create(group=incomplete, user=self.rider)
        RideMember.objects.create(group=completed, user=self.rider)

        response = self.client.get('/rides/history/')
        self.assertContains(response, 'Past ride')
        self.assertNotContains(response, 'Current ride')

    def test_history_includes_recorded_start_route_and_travel_distance(self):
        started_at = timezone.now() - timedelta(minutes=40)
        ride = RideGroup.objects.create(
            name='Recorded ride', created_by=self.rider, status='COMPLETED',
            destination_name='Selected finish', start_latitude=1.0, start_longitude=2.0,
            route_distance_meters=3500, route_duration_seconds=1200,
            started_at=started_at, ended_at=started_at + timedelta(minutes=30),
        )
        member = RideMember.objects.create(group=ride, user=self.rider)
        LocationLog.objects.create(
            ride_member=member, latitude=1.0, longitude=2.0,
            timestamp=started_at,
        )
        LocationLog.objects.create(
            ride_member=member, latitude=1.0, longitude=2.01,
            timestamp=started_at + timedelta(minutes=5),
        )

        response = self.client.get('/api/rides/history/')
        ride_data = response.json()['rides'][0]

        self.assertEqual(ride_data['start_location'], '1.00000, 2.00000')
        self.assertEqual(ride_data['route_distance_km'], 3.5)
        self.assertGreater(ride_data['travelled_distance_km'], 1.0)
        self.assertEqual(ride_data['duration_minutes'], 30)

    def test_create_ride_requires_destination(self):
        response = self.client.post('/groups/create/', {
            'name': 'Morning ride',
            'destination_name': '',
        })

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error'], 'Please select a destination.')
        self.assertFalse(RideGroup.objects.exists())

    def test_create_ride_saves_map_destination_coordinates(self):
        response = self.client.post('/groups/create/', {
            'name': 'Hill ride',
            'destination_name': 'Selected destination',
            'destination_latitude': '1.234500',
            'destination_longitude': '2.345600',
            'start_latitude': '1.200000',
            'start_longitude': '2.300000',
            'planned_route': json.dumps([[1.2, 2.3], [1.2345, 2.3456]]),
            'route_distance_meters': '6500',
            'route_duration_seconds': '900.4',
        })

        self.assertEqual(response.status_code, 200)
        ride = RideGroup.objects.get()
        self.assertEqual(ride.destination_name, 'Selected destination')
        self.assertEqual(float(ride.destination_latitude), 1.2345)
        self.assertEqual(float(ride.destination_longitude), 2.3456)
        self.assertEqual(ride.status, 'READY')
        self.assertEqual(ride.planned_route, [[1.2, 2.3], [1.2345, 2.3456]])
        self.assertEqual(ride.route_distance_meters, 6500)
        self.assertEqual(ride.route_duration_seconds, 901)

    def test_create_ride_rejects_missing_gps_or_road_route(self):
        response = self.client.post('/groups/create/', {
            'name': 'Incomplete ride',
            'destination_name': 'Selected destination',
            'destination_latitude': '1.234500',
            'destination_longitude': '2.345600',
        })

        self.assertEqual(response.status_code, 400)
        self.assertFalse(RideGroup.objects.exists())

    def test_start_ride_requires_route_and_persists_fresh_start_location(self):
        ride = RideGroup.objects.create(
            name='Ready ride', created_by=self.rider, status='READY',
            destination_name='Selected destination',
            destination_latitude='1.234500', destination_longitude='2.345600',
        )
        RideMember.objects.create(group=ride, user=self.rider, role='LEADER', status='ACTIVE')

        incomplete = self.client.post(f'/api/groups/{ride.id}/start/', data='{}', content_type='application/json')
        self.assertEqual(incomplete.status_code, 400)
        self.assertEqual(ride.status, 'READY')

        mismatched_route = {
            'start_latitude': 1.2,
            'start_longitude': 2.3,
            'planned_route': [[15.0, 26.0], [15.1, 26.1]],
            'route_distance_meters': 6500,
            'route_duration_seconds': 900.4,
        }
        mismatched = self.client.post(
            f'/api/groups/{ride.id}/start/', data=json.dumps(mismatched_route), content_type='application/json',
        )
        self.assertEqual(mismatched.status_code, 400)

        payload = {
            'start_latitude': 1.2,
            'start_longitude': 2.3,
            'planned_route': [[1.2, 2.3], [1.2345, 2.3456]],
            'route_distance_meters': 6500,
            'route_duration_seconds': 900.4,
        }
        response = self.client.post(
            f'/api/groups/{ride.id}/start/', data=json.dumps(payload), content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        ride.refresh_from_db()
        self.assertEqual(ride.status, 'ACTIVE')
        self.assertAlmostEqual(ride.start_latitude, 1.2)
        self.assertAlmostEqual(ride.start_longitude, 2.3)
        self.assertEqual(ride.route_distance_meters, 6500)
        self.assertEqual(ride.route_duration_seconds, 901)

    def test_completed_group_is_not_loaded_as_dashboard_active_pack(self):
        completed = RideGroup.objects.create(
            name='Earlier ride', created_by=self.rider, status='COMPLETED',
            destination_name='Old destination',
            destination_latitude='12.345678', destination_longitude='98.765432',
            start_latitude=12.0, start_longitude=98.0,
        )
        RideMember.objects.create(group=completed, user=self.rider, role='LEADER', status='ENDED')

        response = self.client.get(f'/dashboard/{completed.id}/')
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context['active_pack'])
        self.assertNotContains(response, 'Old destination')
        self.assertContains(response, 'No active destination')
        self.assertContains(response, 'No active ride')

    def test_dashboard_waypoint_uses_active_ride_only(self):
        completed = RideGroup.objects.create(
            name='Earlier ride', created_by=self.rider, status='COMPLETED',
            destination_name='Old destination',
        )
        response = self.client.get('/dashboard/')
        self.assertNotContains(response, 'Old destination')
        self.assertContains(response, 'No active destination')
        self.assertContains(response, '0 / 0')
        self.assertContains(response, '0 km/h')
        self.assertContains(response, '0m 00s')
        self.assertContains(response, 'No riders currently active')
        self.assertContains(response, 'Select on Map')
        self.assertContains(response, 'No rides yet')

        response = self.client.get(f'/dashboard/{completed.id}/')
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Old destination')

        ready = RideGroup.objects.create(
            name='Ready ride', created_by=self.rider, status='READY',
            destination_name='Not active yet',
        )
        response = self.client.get(f'/dashboard/{ready.id}/')
        self.assertContains(response, 'Start Ride')
        dashboard_html = response.content.decode()
        self.assertEqual(dashboard_html.count('id="btn-start-ride"'), 1)
        self.assertIn(f'onclick="startRideNow({ready.id})"', dashboard_html)
        self.assertNotRegex(dashboard_html, r'<button\b[^>]*>\s*Start\s*</button>')
        self.assertContains(response, 'No active destination')
        self.assertNotContains(response, 'Not active yet')

        current = RideGroup.objects.create(
            name='Current ride', created_by=self.rider, status='ACTIVE',
            destination_name='Current destination',
        )
        RideGroup.objects.create(
            name='Newer ready ride', created_by=self.rider, status='READY',
            destination_name='Next destination',
        )
        response = self.client.get('/dashboard/')
        self.assertContains(response, 'Current destination')
        self.assertNotContains(response, 'Old destination')
        self.assertNotContains(response, 'Next destination')

        response = self.client.get(f'/dashboard/{current.id}/')
        self.assertContains(response, 'Current destination')

    def test_dashboard_prefers_user_membership_over_unrelated_active_group(self):
        rider = User.objects.create_user(username='member-rider', password='Password123!')
        owner = User.objects.create_user(username='owner-rider', password='Password123!')

        unrelated_active = RideGroup.objects.create(
            name='Unrelated active ride', created_by=owner, status='ACTIVE',
            destination_name='Other destination',
        )
        joined_ready = RideGroup.objects.create(
            name='Joined ride', created_by=owner, status='READY',
            destination_name='Joined destination',
        )
        RideMember.objects.create(group=joined_ready, user=rider, role='RIDER', status='ACTIVE')

        dashboard_group = resolve_dashboard_group_for_user(
            rider,
            requested_pack_id=None,
            groups=RideGroup.objects.filter(id__in=[unrelated_active.id, joined_ready.id]),
        )

        self.assertEqual(dashboard_group.id, joined_ready.id)

    def test_dashboard_loads_members_for_ready_ride_before_start(self):
        ride = RideGroup.objects.create(
            name='Waiting ride', created_by=self.rider, status='READY',
            destination_name='Waiting destination',
        )
        leader_member = RideMember.objects.create(
            group=ride, user=self.rider, role='LEADER', status='ACTIVE',
        )
        rider_member = User.objects.create_user(username='waiting-rider', password='Password123!')
        RideMember.objects.create(
            group=ride, user=rider_member, role='RIDER', status='ACTIVE',
        )

        self.client.force_login(rider_member)
        response = self.client.get(f'/dashboard/{ride.id}/')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Waiting ride')
        self.assertContains(response, 'Riders')
        self.assertContains(response, 'Ready')
        self.assertContains(response, self.rider.username)
        self.assertContains(response, rider_member.username)

    def test_dashboard_renders_distinct_start_and_destination_markers(self):
        ride = RideGroup.objects.create(
            name='Marker ride', created_by=self.rider, status='ACTIVE',
            destination_name='Marker destination',
            destination_latitude='11.123456', destination_longitude='76.654321',
            start_latitude=11.0, start_longitude=76.6,
        )
        RideMember.objects.create(
            group=ride, user=self.rider, role='LEADER', status='ACTIVE',
        )

        response = self.client.get(f'/dashboard/{ride.id}/')
        html = response.content.decode()

        self.assertIn('const START_POSITION', html)
        self.assertIn('const DESTINATION_POSITION', html)
        self.assertIn("text: 'S'", html)
        self.assertIn("text: 'D'", html)

    def test_ending_ride_clears_dashboard_destination_and_keeps_history(self):
        ride = RideGroup.objects.create(
            name='Completed ride', created_by=self.rider, status='ACTIVE',
            destination_name='Sunset viewpoint',
        )
        RideMember.objects.create(
            group=ride, user=self.rider, role='LEADER', status='ACTIVE',
        )

        response = self.client.post(f'/api/groups/{ride.id}/end/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'COMPLETED')
        self.assertNotContains(self.client.get('/dashboard/'), 'Sunset viewpoint')
        self.assertContains(self.client.get('/rides/history/'), 'Sunset viewpoint')


class SOSTests(TestCase):
    def setUp(self):
        self.leader = User.objects.create_user(username='leader-sos', password='Password123!')
        self.rider = User.objects.create_user(username='rider-sos', password='Password123!')
        self.group = RideGroup.objects.create(
            name='SOS ride',
            created_by=self.leader,
            status='READY',
            destination_name='SOS destination',
            destination_latitude='12.500000',
            destination_longitude='77.500000',
            start_latitude=12.0,
            start_longitude=77.0,
            planned_route=[[12.0, 77.0], [12.5, 77.5]],
            route_distance_meters=1500,
            route_duration_seconds=600,
        )
        RideMember.objects.create(group=self.group, user=self.leader, role='LEADER', status='ACTIVE', is_connected=True)
        RideMember.objects.create(group=self.group, user=self.rider, role='RIDER', status='ACTIVE', is_connected=True)

    def test_start_ride_does_not_create_sos_alert(self):
        self.client.force_login(self.leader)
        payload = {
            'start_latitude': 12.1,
            'start_longitude': 77.1,
            'planned_route': [[12.1, 77.1], [12.5, 77.5]],
            'route_distance_meters': 1500,
            'route_duration_seconds': 600,
        }

        response = self.client.post(f'/api/groups/{self.group.id}/start/', data=json.dumps(payload), content_type='application/json')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.group.sos_alerts.count(), 0)

    def test_explicit_sos_is_bound_to_triggering_rider_only(self):
        self.group.status = 'ACTIVE'
        self.group.save(update_fields=['status'])
        self.client.force_login(self.rider)

        response = self.client.post(
            f'/api/packs/{self.group.id}/sos/',
            {'latitude': '12.35', 'longitude': '77.35', 'message': 'Need help now'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['rider_id'], self.rider.id)
        self.assertEqual(response.json()['rider_name'], self.rider.username)
        alert = self.group.sos_alerts.get(status='ACTIVE')
        self.assertEqual(alert.ride_member.user, self.rider)
        self.assertEqual(alert.ride_member.group, self.group)
        self.assertEqual(self.group.sos_alerts.count(), 1)

    def test_other_ride_does_not_receive_sender_sos_alert(self):
        other_group = RideGroup.objects.create(
            name='Other SOS ride',
            created_by=self.leader,
            status='ACTIVE',
            destination_name='Other destination',
            destination_latitude='12.600000',
            destination_longitude='77.600000',
        )
        RideMember.objects.create(group=other_group, user=self.leader, role='LEADER', status='ACTIVE', is_connected=True)
        self.group.status = 'ACTIVE'
        self.group.save(update_fields=['status'])
        self.client.force_login(self.rider)

        response = self.client.post(
            f'/api/packs/{self.group.id}/sos/',
            {'latitude': '12.35', 'longitude': '77.35', 'message': 'Need help now'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.group.sos_alerts.count(), 1)
        self.assertEqual(other_group.sos_alerts.count(), 0)

    def test_dashboard_refresh_keeps_active_sos_for_the_correct_rider(self):
        self.group.status = 'ACTIVE'
        self.group.save(update_fields=['status'])
        other_rider = User.objects.create_user(username='other-sos-rider', password='Password123!')
        RideMember.objects.create(group=self.group, user=other_rider, role='RIDER', status='ACTIVE', is_connected=True)
        SOSAlert.objects.create(
            ride_member=self.group.members.get(user=self.rider),
            latitude=12.35,
            longitude=77.35,
            message='Need help now',
            status='ACTIVE',
        )

        self.client.force_login(self.rider)
        response = self.client.get(f'/dashboard/{self.group.id}/')
        html = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'ACTIVE SOS ALERT')
        self.assertContains(response, 'Need help now')
        self.assertIn('Rider 2 · rider-sos: Need help now', html)
        self.assertNotIn(f'Rider 1 · {self.leader.username}: Need help now', html)

    def test_disconnected_member_is_reactivated_when_dashboard_is_opened(self):
        self.group.status = 'ACTIVE'
        self.group.save(update_fields=['status'])
        member = self.group.members.get(user=self.rider)
        member.status = 'DISCONNECTED'
        member.is_connected = False
        member.save(update_fields=['status', 'is_connected'])

        self.client.force_login(self.rider)
        response = self.client.get(f'/dashboard/{self.group.id}/')
        self.assertEqual(response.status_code, 200)

        member.refresh_from_db()
        self.assertEqual(member.status, 'ACTIVE')
        self.assertTrue(member.is_connected)

        sos_response = self.client.post(
            f'/api/packs/{self.group.id}/sos/',
            {'latitude': '12.35', 'longitude': '77.35', 'message': 'Need help now'},
        )
        self.assertEqual(sos_response.status_code, 200)
        self.assertEqual(sos_response.json()['rider_id'], self.rider.id)

    def test_end_ride_clears_active_membership_state_but_preserves_history(self):
        self.group.status = 'ACTIVE'
        self.group.save(update_fields=['status'])
        self.client.force_login(self.leader)

        response = self.client.post(f'/api/groups/{self.group.id}/end/')

        self.assertEqual(response.status_code, 200)
        self.group.refresh_from_db()
        self.assertEqual(self.group.status, 'COMPLETED')
        self.assertIsNotNone(self.group.ended_at)
        self.assertFalse(self.group.members.filter(status='ACTIVE').exists())
        self.assertEqual(self.group.members.filter(status='ENDED').count(), 2)
        self.assertTrue(RideGroup.objects.filter(pk=self.group.id).exists())

    def test_rider_leaving_does_not_terminate_entire_ride(self):
        self.group.status = 'ACTIVE'
        self.group.save(update_fields=['status'])
        self.client.force_login(self.rider)

        response = self.client.post(f'/api/groups/{self.group.id}/leave/')

        self.assertEqual(response.status_code, 200)
        self.group.refresh_from_db()
        self.assertEqual(self.group.status, 'ACTIVE')
        self.assertEqual(self.group.members.get(user=self.rider).status, 'LEFT')
        self.assertTrue(self.group.members.filter(status='ACTIVE').exists())


class RideIdentityTests(TestCase):
    def setUp(self):
        self.leader = User.objects.create_user(username='identity-leader', first_name='Alan')
        self.group = RideGroup.objects.create(name='Identity ride', created_by=self.leader, status='ACTIVE')
        self.leader_member = RideMember.objects.create(
            group=self.group, user=self.leader, role='LEADER', status='ACTIVE',
        )
        self.riders = []
        for username in ('identity-titus', 'identity-thanuj'):
            user = User.objects.create_user(username=username)
            self.riders.append(RideMember.objects.create(
                group=self.group, user=user, role='RIDER', status='ACTIVE',
            ))

    def test_numbers_and_colors_are_stable_and_shared_by_dashboard_and_live_state(self):
        second_member, third_member = self.riders
        second_member.status = 'DISCONNECTED'
        second_member.is_connected = False
        second_member.save(update_fields=['status', 'is_connected'])
        fourth_user = User.objects.create_user(username='identity-rahul')
        fourth_member = RideMember.objects.create(group=self.group, user=fourth_user, role='RIDER', status='ACTIVE')

        self.assertEqual(
            [self.leader_member.rider_number, second_member.rider_number, third_member.rider_number, fourth_member.rider_number],
            [1, 2, 3, 4],
        )
        from tracker.services import get_rider_color
        rider_colors = [get_rider_color(number) for number in range(1, 9)]
        self.assertEqual(len(set(rider_colors)), 8)

        self.client.force_login(self.leader)
        dashboard = self.client.get(f'/dashboard/{self.group.id}/')
        live_state = self.client.get(f'/api/groups/{self.group.id}/live-state/').json()
        dashboard_members = dashboard.context['members']
        live_members = live_state['members']
        self.assertEqual([member['rider_number'] for member in dashboard_members], [1, 2, 3, 4])
        self.assertEqual([member['rider_number'] for member in live_members], [1, 2, 3, 4])
        self.assertEqual(
            [member['color'] for member in dashboard_members],
            [member['color'] for member in live_members],
        )
        html = dashboard.content.decode()
        for rider_number, color in enumerate(rider_colors[:4], start=1):
            self.assertIn(f'Rider {rider_number}', html)
            self.assertIn(color, html)
        self.assertIn('label: { text: String(riderInfo.rider_number || \'?\')', html)
        self.assertIn('riderMarkers[id]', html)

    def test_rejoining_member_keeps_number_and_new_member_gets_next_number(self):
        second_member = self.riders[0]
        second_member.status = 'DISCONNECTED'
        second_member.is_connected = False
        second_member.save(update_fields=['status', 'is_connected'])
        self.client.force_login(second_member.user)
        response = self.client.post(
            '/api/groups/join/',
            data=json.dumps({'join_code': self.group.join_code}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        second_member.refresh_from_db()
        self.assertEqual(second_member.rider_number, 2)

        fourth_user = User.objects.create_user(username='identity-fourth')
        fourth_member = RideMember.objects.create(group=self.group, user=fourth_user, role='RIDER', status='ACTIVE')
        self.assertEqual(fourth_member.rider_number, 4)


class HTTPFallbackLocationTests(TransactionTestCase):
    def test_http_gps_fallback_broadcasts_authenticated_membership_identity(self):
        user = User.objects.create_user(username='http-gps-rider', first_name='Titus')
        group = RideGroup.objects.create(name='HTTP GPS ride', created_by=user, status='ACTIVE')
        member = RideMember.objects.create(group=group, user=user, role='LEADER', status='ACTIVE')
        channel_layer = get_channel_layer()
        channel_name = async_to_sync(channel_layer.new_channel)()
        room = f'ride_group_{group.id}'
        async_to_sync(channel_layer.group_add)(room, channel_name)

        self.client.force_login(user)
        try:
            response = self.client.post(
                f'/api/groups/{group.id}/location/',
                data=json.dumps({'latitude': 12.35, 'longitude': 77.35, 'speed': 24}),
                content_type='application/json',
            )
            event = async_to_sync(channel_layer.receive)(channel_name)
        finally:
            async_to_sync(channel_layer.group_discard)(room, channel_name)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(event['type'], 'broadcast_location')
        self.assertEqual(event['message']['rider_id'], user.id)
        self.assertEqual(event['message']['rider_number'], member.rider_number)
        self.assertEqual(event['message']['name'], 'Titus')
        self.assertEqual(event['message']['color'], '#4C7EFF')
        self.assertEqual(event['message']['latitude'], 12.35)
        self.assertEqual(event['message']['longitude'], 77.35)


class RideSOSWebSocketTests(TransactionTestCase):
    def setUp(self):
        self.sender = User.objects.create_user(username='sos-sender', first_name='Titus')
        self.same_ride = User.objects.create_user(username='same-ride-rider')
        self.other_ride = User.objects.create_user(username='other-ride-rider')
        self.ride = RideGroup.objects.create(name='Live SOS ride', created_by=self.sender, status='ACTIVE')
        self.separate_ride = RideGroup.objects.create(name='Separate ride', created_by=self.other_ride, status='ACTIVE')
        RideMember.objects.create(group=self.ride, user=self.sender, role='LEADER', status='ACTIVE')
        RideMember.objects.create(group=self.ride, user=self.same_ride, role='RIDER', status='ACTIVE')
        RideMember.objects.create(group=self.separate_ride, user=self.other_ride, role='LEADER', status='ACTIVE')

    def make_communicator(self, user, group):
        consumer_app = RideTrackingConsumer.as_asgi()

        async def authenticated_consumer(scope, receive, send):
            scope = {
                **scope,
                'user': user,
                'url_route': {'kwargs': {'group_id': str(group.id)}},
            }
            await consumer_app(scope, receive, send)

        return WebsocketCommunicator(authenticated_consumer, f'/ws/rides/{group.id}/')

    def test_sos_broadcasts_to_same_ride_with_authenticated_identity_only(self):
        async_to_sync(self._assert_ride_scoped_sos_broadcast)()
        alert = SOSAlert.objects.get()
        self.assertEqual(alert.ride_member.user, self.sender)
        self.assertEqual(alert.ride_member.group, self.ride)

    def test_location_updates_from_any_member_are_broadcast_to_same_ride_group(self):
        async_to_sync(self._assert_location_update_sync)()

    async def _assert_location_update_sync(self):
        sender_socket = self.make_communicator(self.sender, self.ride)
        same_ride_socket = self.make_communicator(self.same_ride, self.ride)
        other_ride_socket = self.make_communicator(self.other_ride, self.separate_ride)

        async def receive_location(socket, expected_rider_id=None):
            for _ in range(20):
                payload = await socket.receive_json_from(timeout=2)
                if payload.get('type') == 'location_update':
                    if expected_rider_id is None or payload.get('rider_id') == expected_rider_id:
                        return payload
            self.fail('Location update was not delivered to the expected rider on this ride.')

        try:
            for socket in (sender_socket, same_ride_socket, other_ride_socket):
                connected, _ = await socket.connect()
                self.assertTrue(connected)

            await sender_socket.send_json_to({
                'type': 'location_update',
                'latitude': 12.35,
                'longitude': 77.35,
                'speed': 24,
                'accuracy': 5,
                'heading': 90,
            })
            sender_payload = await receive_location(same_ride_socket, self.sender.id)
            self.assertEqual(sender_payload['ride_id'], self.ride.id)
            self.assertEqual(sender_payload['rider_id'], self.sender.id)
            self.assertEqual(sender_payload['latitude'], 12.35)
            self.assertEqual(sender_payload['longitude'], 77.35)

            await same_ride_socket.send_json_to({
                'type': 'location_update',
                'latitude': 22.35,
                'longitude': 78.35,
                'speed': 27,
                'accuracy': 6,
                'heading': 180,
            })
            rider_payload = await receive_location(sender_socket, self.same_ride.id)
            self.assertEqual(rider_payload['ride_id'], self.ride.id)
            self.assertEqual(rider_payload['rider_id'], self.same_ride.id)
            self.assertEqual(rider_payload['latitude'], 22.35)
            self.assertEqual(rider_payload['longitude'], 78.35)
        finally:
            for socket in (sender_socket, same_ride_socket, other_ride_socket):
                await socket.disconnect()

    async def _assert_ride_scoped_sos_broadcast(self):
        sender_socket = self.make_communicator(self.sender, self.ride)
        same_ride_socket = self.make_communicator(self.same_ride, self.ride)
        other_ride_socket = self.make_communicator(self.other_ride, self.separate_ride)
        sockets = [sender_socket, same_ride_socket, other_ride_socket]

        async def receive_sos(socket):
            for _ in range(10):
                payload = await socket.receive_json_from(timeout=2)
                if payload.get('type') == 'sos_alert':
                    return payload
            self.fail('SOS event was not delivered to this ride member.')

        try:
            for socket in sockets:
                connected, _ = await socket.connect()
                self.assertTrue(connected)

            other_status = await other_ride_socket.receive_json_from(timeout=1)
            self.assertEqual(other_status['type'], 'rider_status_update')

            await sender_socket.send_json_to({
                'type': 'sos_alert',
                'rider_id': 999999,
                'rider_name': 'Spoofed Sender',
                'message': 'Need assistance. My bike has stopped.',
                'latitude': 12.35,
                'longitude': 77.35,
            })

            payloads = [await receive_sos(socket) for socket in (sender_socket, same_ride_socket)]
            for payload in payloads:
                self.assertEqual(payload['type'], 'sos_alert')
                self.assertEqual(payload['ride_id'], self.ride.id)
                self.assertEqual(payload['rider_id'], self.sender.id)
                self.assertEqual(payload['rider_number'], 1)
                self.assertEqual(payload['rider_color'], '#4C7EFF')
                self.assertEqual(payload['rider_name'], 'Titus')
                self.assertNotIn('Spoofed Sender', payload.values())
                self.assertEqual(payload['message'], 'Need assistance. My bike has stopped.')
                self.assertEqual(payload['latitude'], 12.35)
                self.assertEqual(payload['longitude'], 77.35)
                self.assertTrue(payload['alert_id'])

            await same_ride_socket.send_json_to({
                'type': 'location_update',
                'latitude': 22.35,
                'longitude': 78.35,
                'speed': 27,
            })
            location_payload = None
            for _ in range(10):
                candidate = await sender_socket.receive_json_from(timeout=2)
                if candidate.get('type') == 'location_update':
                    location_payload = candidate
                    break
            self.assertIsNotNone(location_payload)
            self.assertEqual(location_payload['rider_id'], self.same_ride.id)
            self.assertEqual(location_payload['rider_number'], 2)
            self.assertEqual(location_payload['color'], '#E5595E')
            self.assertEqual(location_payload['latitude'], 22.35)
            self.assertEqual(location_payload['longitude'], 78.35)

            await asyncio.sleep(0.1)
            self.assertTrue(other_ride_socket.output_queue.empty())
        finally:
            for socket in sockets:
                try:
                    await socket.disconnect()
                except asyncio.CancelledError:
                    pass


class RegistrationFlowTests(TestCase):
    def test_user_is_created_by_registration_before_a_ride_can_be_created(self):
        self.assertEqual(User.objects.count(), 0)

        response = self.client.post('/register/', {
            'fullname': 'Registered Rider',
            'email': 'registered-rider@example.test',
            'phone': '+1 555 010 1000',
            'vehicle': 'Bicycle',
            'password': 'SecurePass123!',
            'confirm': 'SecurePass123!',
            'terms': 'on',
        })

        self.assertEqual(response.status_code, 302)
        rider = User.objects.get(username='registered-rider@example.test')
        self.assertEqual(rider.profile.ride_type, 'Bicycle')
        self.assertFalse(RideGroup.objects.exists())

        response = self.client.post('/groups/create/', {
            'name': 'Registered ride',
            'destination_name': 'Chosen destination',
            'destination_latitude': '1.234500',
            'destination_longitude': '2.345600',
            'start_latitude': '1.200000',
            'start_longitude': '2.300000',
            'planned_route': json.dumps([[1.2, 2.3], [1.2345, 2.3456]]),
            'route_distance_meters': '6500',
            'route_duration_seconds': '900',
        })
        self.assertEqual(response.status_code, 200)
        ride = RideGroup.objects.get()
        self.assertEqual(ride.created_by, rider)
        self.assertEqual(ride.destination_name, 'Chosen destination')
        self.assertEqual(ride.status, 'READY')


class OsrmRoutingTests(TestCase):
    def setUp(self):
        self.rider = User.objects.create_user(username='osrm-rider', password='Password123!')
        self.client.force_login(self.rider)

    def test_route_endpoint_calls_osrm_with_longitude_first_and_converts_response_fields(self):
        osrm_response = {
            'code': 'Ok',
            'routes': [{
                'distance': 24600.5,
                'duration': 2280,
                'geometry': {
                    'type': 'LineString',
                    'coordinates': [[-73.99, 40.72], [-73.98, 40.73]],
                },
            }],
        }
        with patch('tracker.routing_service.urlopen') as urlopen, self.settings(
            OSRM_BASE_URL='https://router.example.test',
        ):
            urlopen.return_value.__enter__.return_value.read.return_value = json.dumps(osrm_response).encode()
            response = self.client.post('/api/route/', {
                'start_lat': 40.72,
                'start_lng': -73.99,
                'destination_lat': 40.73,
                'destination_lng': -73.98,
            }, content_type='application/json')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            'success': True,
            'distance_meters': 24600.5,
            'duration_seconds': 2280,
            'route_coordinates': [[-73.99, 40.72], [-73.98, 40.73]],
        })
        request = urlopen.call_args.args[0]
        self.assertIn('/route/v1/driving/-73.9900000,40.7200000;-73.9800000,40.7300000?', request.full_url)
        self.assertIn('overview=full', request.full_url)
        self.assertIn('geometries=geojson', request.full_url)
        self.assertEqual(request.get_header('User-agent'), 'PackTrace/1.0')
        self.assertTrue(request.get_header('Referer'))

    def test_route_endpoint_rejects_invalid_coordinates_without_calling_osrm(self):
        with patch('tracker.views.calculate_osrm_route') as calculate_route:
            response = self.client.post('/api/route/', {
                'start_lat': 91,
                'start_lng': 2,
                'destination_lat': 3,
                'destination_lng': 4,
            }, content_type='application/json')

        self.assertEqual(response.status_code, 400)
        calculate_route.assert_not_called()

    def test_route_endpoint_requires_authentication(self):
        self.client.logout()
        response = self.client.post('/api/route/', {
            'start_lat': 40.72,
            'start_lng': -73.99,
            'destination_lat': 40.73,
            'destination_lng': -73.98,
        }, content_type='application/json')

        self.assertEqual(response.status_code, 302)

    def test_osrm_network_failure_returns_service_unavailable_error(self):
        with patch('tracker.routing_service.urlopen', side_effect=URLError('offline')):
            with self.assertRaises(RouteServiceError) as error:
                calculate_osrm_route(40.72, -73.99, 40.73, -73.98)

        self.assertEqual(error.exception.status, 502)

    def test_osrm_no_route_response_is_reported_without_fabricating_geometry(self):
        with patch('tracker.routing_service.urlopen') as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = json.dumps({
                'code': 'NoRoute', 'routes': [],
            }).encode()
            with self.assertRaises(RouteServiceError) as error:
                calculate_osrm_route(40.72, -73.99, 40.73, -73.98)

        self.assertEqual(error.exception.status, 422)

    def test_route_endpoint_returns_timeout_status_from_upstream(self):
        with patch('tracker.views.calculate_osrm_route', side_effect=RouteServiceError('The routing service timed out.', 504)):
            response = self.client.post('/api/route/', {
                'start_lat': 40.72,
                'start_lng': -73.99,
                'destination_lat': 40.73,
                'destination_lng': -73.98,
            }, content_type='application/json')

        self.assertEqual(response.status_code, 504)
        self.assertFalse(response.json()['success'])

class DisconnectDetectionTests(TransactionTestCase):
    def setUp(self):
        # 1. Active Rider
        self.rider = User.objects.create_user(
            username='disconnect-test-rider',
            email='disconnect-test-rider@example.test',
            password='Password123!',
            first_name='Rider Vikram'
        )
        self.rider.profile.phone = '+91 99887 76655'
        self.rider.profile.ride_type = 'Motorcycle'
        self.rider.profile.save()

        # 2. Active Ride Group
        self.group = RideGroup.objects.create(
            name='Disconnect test ride',
            join_code='DISC01',
            created_by=self.rider,
            status='ACTIVE',
            started_at=timezone.now()
        )

        self.member = RideMember.objects.create(
            group=self.group,
            user=self.rider,
            role='LEADER',
            status='ACTIVE',
            is_connected=True,
            last_seen=timezone.now() - timedelta(seconds=DISCONNECT_THRESHOLD + 10)
        )

        self.loc = LocationLog.objects.create(
            ride_member=self.member,
            latitude=1.2345,
            longitude=2.3456,
            speed=40.0
        )

    def test_stale_rider_marked_disconnected_and_broadcasts(self):
        """Rider whose last_seen exceeds threshold is marked DISCONNECTED with last known coordinates preserved."""
        disconnected = check_and_update_disconnected_riders(self.group)

        self.assertEqual(len(disconnected), 1)
        disc_info = disconnected[0]
        self.assertEqual(disc_info['type'], 'rider_disconnected')
        self.assertEqual(disc_info['rider_id'], self.rider.id)
        self.assertEqual(disc_info['last_latitude'], 1.2345)
        self.assertEqual(disc_info['last_longitude'], 2.3456)

        # Verify database update
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_connected)
        self.assertEqual(self.member.status, 'DISCONNECTED')

    def test_reconnect_restores_active_status(self):
        """When disconnected rider sends update, handle_rider_reconnect restores ACTIVE status and is_connected=True."""
        # Mark disconnected first
        self.member.is_connected = False
        self.member.status = 'DISCONNECTED'
        self.member.save()

        # Rider reconnects
        handle_rider_reconnect(self.member)

        self.member.refresh_from_db()
        self.assertTrue(self.member.is_connected)
        self.assertEqual(self.member.status, 'ACTIVE')
        self.assertIsNotNone(self.member.last_seen)

    def test_configurable_threshold_value(self):
        """DISCONNECT_THRESHOLD is read from settings and is greater than zero."""
        self.assertTrue(DISCONNECT_THRESHOLD >= 30)
        self.assertEqual(DISCONNECT_THRESHOLD, settings.DISCONNECT_THRESHOLD_SECONDS)
