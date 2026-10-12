from django.db import migrations, models


def assign_existing_rider_numbers(apps, schema_editor):
    RideMember = apps.get_model('tracker', 'RideMember')
    database = schema_editor.connection.alias
    group_ids = RideMember.objects.using(database).values_list('group_id', flat=True).distinct()
    for group_id in group_ids.iterator():
        members = RideMember.objects.using(database).filter(group_id=group_id).order_by('joined_at', 'pk')
        for number, member in enumerate(members.iterator(), start=1):
            RideMember.objects.using(database).filter(pk=member.pk).update(rider_number=number)


class Migration(migrations.Migration):
    dependencies = [
        ('tracker', '0006_ridegroup_route_metrics_and_start_location'),
    ]

    operations = [
        migrations.AddField(
            model_name='ridemember',
            name='rider_number',
            field=models.PositiveIntegerField(blank=True, editable=False, null=True),
        ),
        migrations.RunPython(assign_existing_rider_numbers, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='ridemember',
            name='rider_number',
            field=models.PositiveIntegerField(editable=False),
        ),
        migrations.AddConstraint(
            model_name='ridemember',
            constraint=models.UniqueConstraint(fields=('group', 'rider_number'), name='uniq_ride_rider_number'),
        ),
    ]