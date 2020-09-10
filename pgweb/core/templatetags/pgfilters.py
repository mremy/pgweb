from django.template.defaultfilters import stringfilter
from django import template
from django.utils.safestring import mark_safe
import json


register = template.Library()


@register.filter(name='class_name')
def class_name(ob):
    return ob.__class__.__name__


@register.filter(is_safe=True)
def field_class(value, arg):
    if 'class' in value.field.widget.attrs:
        c = arg + ' ' + value.field.widget.attrs['class']
    else:
        c = arg
    return value.as_widget(attrs={"class": c})


@register.filter(name='hidemail')
@stringfilter
def hidemail(value):
    return value.replace('@', ' at ')


@register.filter(is_safe=True)
def ischeckbox(obj):
    return obj.field.widget.__class__.__name__ in ["CheckboxInput", "CheckboxSelectMultiple"] and not getattr(obj.field, 'regular_field', False)


@register.filter(is_safe=True)
def ismultiplecheckboxes(obj):
    return obj.field.widget.__class__.__name__ == "CheckboxSelectMultiple" and not getattr(obj.field, 'regular_field', False)


@register.filter(is_safe=True)
def isrequired_error(obj):
    if obj.errors and obj.errors[0] == "This field is required.":
        return True
    return False


@register.filter(is_safe=True)
def label_class(value, arg):
    return value.label_tag(attrs={'class': arg})


@register.filter()
def planet_author(obj):
    # takes a ImportedRSSItem object from a Planet feed and extracts the author
    # information from the title
    return obj.title.split(':')[0]


@register.filter()
def planet_title(obj):
    # takes a ImportedRSSItem object from a Planet feed and extracts the info
    # specific to the title of the Planet entry
    return ":".join(obj.title.split(':')[1:])


@register.filter(name='dictlookup')
def dictlookup(value, key):
    return value.get(key, None)


@register.filter(name='json')
def tojson(value):
    return json.dumps(value)


@register.filter()
def release_notes_pg_minor_version(minor_version, major_version):
    """Formats the minor version number to the appropriate PostgreSQL version.
    This is particularly for very old version of PostgreSQL.
    """
    if str(major_version) in ['0', '1']:
        return str(minor_version)[2:4]
    return minor_version


@register.filter()
def joinandor(value, andor):
    # Value is a list of objects. Join them on comma, add "and" or "or" before the last.
    if len(value) == 1:
        return str(value[0])

    if not isinstance(value, list):
        # Must have a list to index from the end
        value = list(value)

    return ", ".join([str(x) for x in value[:-1]]) + ' ' + andor + ' ' + str(value[-1])


@register.simple_tag(takes_context=True)
def git_changes_link(context):
    return mark_safe('<a href="https://git.postgresql.org/gitweb/?p=pgweb.git;a=history;f=templates/{}">View</a> change history.'.format(context.template_name))
