# -*- coding: utf-8 -*-
# Generated by Django 1.11.27 on 2020-01-03 20:57
from __future__ import unicode_literals

from django.db import migrations


def copy_column_values(apps, schema_editor):
    """
    Copy the values field into the site_values field.
    """
    SiteConfiguration = apps.get_model('site_configuration', 'SiteConfiguration')
    for site_configuration in SiteConfiguration.objects.all():
        site_configuration.site_values = site_configuration.values
        site_configuration.save()


class Migration(migrations.Migration):

    dependencies = [
        ('site_configuration', '0003_add_site_values_field'),
    ]

    operations = [
        migrations.RunPython(
            copy_column_values,
            reverse_code=migrations.RunPython.noop,  # Allow reverse migrations, but make it a no-op.
        ),
    ]
