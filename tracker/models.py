import secrets
import string
from django.db import models, transaction
from django.db.models import Max
from django.contrib.auth.models import User
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

def generate_join_code(length=6):
    """
    Generates a cryptographically secure random alphanumeric join code (e.g. 'PT7K4Q').
    Guarantees uniqueness across active RideGroup instances.
    """
    chars = ''.join([c for c in string.ascii_uppercase + string.digits if c not in '0O1I'])
    while True:
        code = ''.join(secrets.choice(chars) for _ in range(length))
        # Ensure the same join code does not belong to multiple active groups
        if not RideGroup.objects.filter(join_code=code, status__in=['CREATED', 'ACTIVE', 'PAUSED']).exists():
            return code



class RiderProfile(models.Model):
    ROLE_CHOICES = [
        ('rider', 'Rider'),
        ('admin', 'Admin'),
    ]

    RIDE_TYPE_CHOICES = [
        ('Motorcycle', 'Motorcycle'),
        ('Bicycle', 'Bicycle'),
        ('Car', 'Car'),
        ('Other', 'Other'),
        ('On foot', 'On foot'),
    ]

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='rider', db_index=True)
    phone = models.CharField(max_length=30, blank=True)
    ride_type = models.CharField(max_length=30, choices=RIDE_TYPE_CHOICES, blank=True, default='')
    location_consent = models.BooleanField(default=False, help_text="Consent to share live location with pack members")
    avatar_initials = models.CharField(max_length=6, blank=True)
    color_code = models.CharField(max_length=20, default='#3ECF8E')
    is_online = models.BooleanField(default=False, db_index=True)
    last_active = models.DateTimeField(auto_now=True, db_index=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def is_admin(self):
        return self.role == 'admin' or self.user.is_staff or self.user.is_superuser

    @property
    def is_rider(self):
        return self.role == 'rider'

    # Backwards-compatibility aliases and properties
    @property
    def phone_number(self):
        return self.phone

    @phone_number.setter
    def phone_number(self, val):
        self.phone = val

    @property
    def vehicle_type(self):
        return self.ride_type

    @vehicle_type.setter
    def vehicle_type(self, val):
        self.ride_type = val

    @property
    def full_name(self):
        return self.user.get_full_name() or self.user.first_name or self.user.username

    @full_name.setter
    def full_name(self, val):
        self.user.first_name = val
        if self.user.pk:
            self.user.save(update_fields=['first_name'])

    def __str__(self):
        return f"{self.full_name} ({self.get_role_display()} - {self.ride_type})"

    def save(self, *args, **kwargs):
        if not self.avatar_initials:
            name = self.user.get_full_name() or self.user.first_name or self.user.username
            parts = name.strip().split()
            if len(parts) >= 2:
                self.avatar_initials = f"{parts[0][0]}{parts[1][0]}".upper()
            elif parts:
                self.avatar_initials = parts[0][:2].upper()
            else:
                self.avatar_initials = "PT"
        super().save(*args, **kwargs)


@receiver(post_save, sender=User)
def create_or_save_rider_profile(sender, instance, created, **kwargs):
    if created:
        role = 'admin' if (instance.is_staff or instance.is_superuser) else 'rider'
        RiderProfile.objects.get_or_create(user=instance, defaults={'role': role})


class RideGroup(models.Model):
    STATUS_CHOICES = [
        ('READY', 'Ready to Ride'),
        ('ACTIVE', 'Active Live Ride'),
        ('PAUSED', 'Paused'),
        ('COMPLETED', 'Completed'),
        ('ENDED', 'Ended'),
    ]



    name = models.CharField(max_length=200, db_index=True)
    join_code = models.CharField(max_length=12, unique=True, db_index=True, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='created_groups')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='ACTIVE', db_index=True)
    description = models.TextField(blank=True)
    destination_name = models.CharField(max_length=150, blank=True, default='')
    destination_latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    destination_longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    start_latitude = models.FloatField(null=True, blank=True)
    start_longitude = models.FloatField(null=True, blank=True)
    next_waypoint_text = models.CharField(max_length=150, blank=True, default='')
    planned_route = models.JSONField(default=list, blank=True, help_text='Array of [lat, lng] coordinates for the planned route')
    route_distance_meters = models.FloatField(null=True, blank=True)
    route_duration_seconds = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    # Aliases and helper properties
    @property
    def creator(self):
        return self.created_by

    @property
    def memberships(self):
        return self.members

    @property
    def sos_alerts(self):
        return SOSAlert.objects.filter(ride_member__group=self)

    @property
    def alerts(self):
        return self.sos_alerts


    class Meta:
        indexes = [
            models.Index(fields=['status', 'created_at']),
            models.Index(fields=['join_code']),
        ]

    def save(self, *args, **kwargs):
        if not self.join_code:
            code = generate_join_code()
            while RideGroup.objects.filter(join_code=code).exists():
                code = generate_join_code()
            self.join_code = code
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} [{self.join_code}] ({self.get_status_display()})"


class RideMember(models.Model):
    ROLE_CHOICES = [
        ('LEADER', 'Leader'),
        ('RIDER', 'Rider'),
    ]

    STATUS_CHOICES = [
        ('INVITED', 'Invited'),
        ('ACTIVE', 'Active'),
        ('DISCONNECTED', 'Disconnected'),
        ('LEFT', 'Left'),
        ('ENDED', 'Ended'),
    ]

    group = models.ForeignKey(RideGroup, on_delete=models.CASCADE, related_name='members', db_index=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='ride_memberships', db_index=True)
    rider_number = models.PositiveIntegerField(editable=False)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='RIDER', db_index=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='ACTIVE', db_index=True)
    is_connected = models.BooleanField(default=True, db_index=True)
    last_seen = models.DateTimeField(default=timezone.now, db_index=True)
    route_deviation = models.BooleanField(default=False, db_index=True)
    joined_at = models.DateTimeField(default=timezone.now)
    left_at = models.DateTimeField(null=True, blank=True)

    # Aliases
    @property
    def pack(self):
        return self.group

    @property
    def rider(self):
        return self.user

    @property
    def is_active(self):
        return self.status == 'ACTIVE'

    class Meta:
        unique_together = ('group', 'user')
        constraints = [
            models.UniqueConstraint(fields=['group', 'rider_number'], name='uniq_ride_rider_number'),
        ]
        indexes = [
            models.Index(fields=['group', 'status']),
            models.Index(fields=['user', 'status']),
            models.Index(fields=['is_connected', 'last_seen']),
        ]

    def save(self, *args, **kwargs):
        if self._state.adding and self.rider_number is None:
            with transaction.atomic():
                RideGroup.objects.select_for_update().get(pk=self.group_id)
                highest_number = type(self).objects.filter(group_id=self.group_id).aggregate(
                    highest=Max('rider_number')
                )['highest'] or 0
                self.rider_number = highest_number + 1
                return super().save(*args, **kwargs)
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.user.username} in {self.group.name} ({self.role} - {self.status})"


class LocationLog(models.Model):
    ride_member = models.ForeignKey(RideMember, on_delete=models.CASCADE, related_name='location_logs', db_index=True)
    latitude = models.FloatField()
    longitude = models.FloatField()
    speed = models.FloatField(default=0.0)
    accuracy = models.FloatField(null=True, blank=True)
    heading = models.FloatField(default=0.0)
    timestamp = models.DateTimeField(default=timezone.now, db_index=True)

    # Aliases
    @property
    def pack(self):
        return self.ride_member.group

    @property
    def rider(self):
        return self.ride_member.user

    @property
    def speed_kmh(self):
        return self.speed

    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['ride_member', '-timestamp']),
            models.Index(fields=['-timestamp']),
        ]

    def __str__(self):
        return f"{self.ride_member.user.username} @ ({self.latitude}, {self.longitude}) - {self.speed} km/h"


class SOSAlert(models.Model):
    STATUS_CHOICES = [
        ('ACTIVE', 'Active'),
        ('ACKNOWLEDGED', 'Acknowledged'),
        ('RESOLVED', 'Resolved'),
    ]

    ride_member = models.ForeignKey(RideMember, on_delete=models.CASCADE, related_name='sos_alerts', db_index=True)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    message = models.TextField(blank=True)
    timestamp = models.DateTimeField(default=timezone.now, db_index=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='ACTIVE', db_index=True)
    resolved_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='resolved_sos_alerts')
    resolved_at = models.DateTimeField(null=True, blank=True)

    # Aliases
    @property
    def pack(self):
        return self.ride_member.group

    @property
    def rider(self):
        return self.ride_member.user

    @property
    def alert_type(self):
        return 'sos'

    @property
    def created_at(self):
        return self.timestamp

    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['status', '-timestamp']),
            models.Index(fields=['ride_member', 'status']),
        ]

    def __str__(self):
        return f"[SOS] {self.ride_member.user.username} in {self.ride_member.group.name} ({self.status})"


# Model aliases for seamless interoperability
Pack = RideGroup
PackMembership = RideMember
RiderLocation = LocationLog
RideAlert = SOSAlert
