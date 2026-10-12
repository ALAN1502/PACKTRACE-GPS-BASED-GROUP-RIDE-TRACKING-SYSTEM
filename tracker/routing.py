from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    re_path(r'^ws/rides/(?P<group_id>\d+)/$', consumers.RideTrackingConsumer.as_asgi()),
    re_path(r'^ws/groups/(?P<group_id>\d+)/$', consumers.RideTrackingConsumer.as_asgi()),
    re_path(r'^ws/ride/(?P<group_id>\d+)/$', consumers.RideTrackingConsumer.as_asgi()),
]
