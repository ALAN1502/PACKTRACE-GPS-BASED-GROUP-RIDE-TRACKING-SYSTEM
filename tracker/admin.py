from django.contrib import admin
from .models import RiderProfile, RideGroup, RideMember, LocationLog, SOSAlert

@admin.register(RiderProfile)
class RiderProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'full_name', 'role', 'phone', 'ride_type', 'is_online', 'last_active')
    list_filter = ('role', 'ride_type', 'is_online')
    search_fields = ('user__username', 'user__email', 'user__first_name', 'phone')

@admin.register(RideGroup)
class RideGroupAdmin(admin.ModelAdmin):
    list_display = ('name', 'join_code', 'created_by', 'status', 'destination_name', 'created_at')
    list_filter = ('status',)
    search_fields = ('name', 'join_code', 'destination_name')

@admin.register(RideMember)
class RideMemberAdmin(admin.ModelAdmin):
    list_display = ('group', 'user', 'role', 'status', 'is_connected', 'last_seen', 'joined_at')
    list_filter = ('role', 'status', 'is_connected')
    search_fields = ('group__name', 'user__username', 'user__first_name')

@admin.register(LocationLog)
class LocationLogAdmin(admin.ModelAdmin):
    list_display = ('ride_member', 'latitude', 'longitude', 'speed', 'heading', 'timestamp')
    list_filter = ('ride_member__group',)
    search_fields = ('ride_member__user__username', 'ride_member__group__name')

@admin.register(SOSAlert)
class SOSAlertAdmin(admin.ModelAdmin):
    list_display = ('ride_member', 'status', 'message', 'latitude', 'longitude', 'timestamp', 'resolved_by')
    list_filter = ('status',)
    search_fields = ('ride_member__user__username', 'ride_member__group__name', 'message')
