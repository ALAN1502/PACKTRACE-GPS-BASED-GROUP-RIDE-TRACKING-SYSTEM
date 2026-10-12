from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tracker', '0005_alter_riderprofile_is_online_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='ridegroup',
            name='start_latitude',
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='ridegroup',
            name='start_longitude',
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='ridegroup',
            name='route_distance_meters',
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='ridegroup',
            name='route_duration_seconds',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
    ]