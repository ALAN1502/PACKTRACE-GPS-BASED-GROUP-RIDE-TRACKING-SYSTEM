from django.urls import path
from . import views

urlpatterns = [
    # Core Auth & Dashboard
    path('', views.dashboard_view, name='dashboard'),
    path('dashboard/', views.dashboard_view, name='dashboard_home'),
    path('dashboard/<int:pack_id>/', views.dashboard_view, name='dashboard_pack'),
    path('login/', views.login_view, name='login'),
    path('register/', views.register_view, name='register'),
    path('logout/', views.logout_view, name='logout'),

    # Group Management APIs
    path('groups/create/', views.create_group_view, name='create_group'),
    path('api/groups/create/', views.create_group_view, name='api_create_group'),
    path('api/groups/', views.list_user_groups_api, name='api_list_groups'),
    path('api/groups/join/', views.join_group_api_view, name='api_join_group'),
    path('groups/<int:pack_id>/join/', views.join_group_view, name='join_group'),
    path('api/groups/<int:group_id>/', views.view_group_api, name='api_view_group'),
    path('api/groups/<int:group_id>/members/', views.view_group_members_api, name='api_group_members'),
    path('groups/<int:pack_id>/leave/', views.leave_group_view, name='leave_group'),
    path('api/groups/<int:group_id>/leave/', views.leave_group_view, name='api_leave_group'),
    path('rides/<int:pack_id>/start/', views.start_ride_view, name='start_ride'),
    path('api/groups/<int:group_id>/start/', views.start_ride_view, name='api_start_ride'),
    path('api/groups/<int:group_id>/end/', views.end_ride_view, name='api_end_ride'),
    path('api/route/', views.osrm_route_api, name='calculate_route'),
    path('api/groups/<int:group_id>/live-state/', views.group_live_state_api, name='api_group_live_state'),
    path('api/groups/<int:group_id>/location/', views.update_location_api, name='api_update_group_location'),
    path('api/location/update/', views.update_location_api, name='api_location_update'),



    # Ride History & Alerts
    path('rides/history/', views.ride_history_view, name='ride_history'),
    path('api/rides/history/', views.ride_history_view, name='api_ride_history'),
    path('api/packs/<int:pack_id>/sos/', views.send_sos_view, name='send_sos'),

    # Admin actions (Protected by @admin_required)
    path('admin-monitor/', views.admin_monitor_view, name='admin_monitor'),
    path('api/alerts/<int:alert_id>/acknowledge/', views.acknowledge_alert_view, name='acknowledge_alert'),
    path('api/alerts/<int:alert_id>/resolve/', views.resolve_alert_view, name='resolve_alert'),
    path('api/admin/overview/', views.admin_overview_api, name='admin_overview_api'),
]
