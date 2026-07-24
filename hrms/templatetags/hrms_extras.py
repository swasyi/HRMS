import calendar

from django import template

register = template.Library()


@register.filter
def month_name(value):
    try:
        return calendar.month_name[int(value)]
    except (ValueError, TypeError, IndexError):
        return value
