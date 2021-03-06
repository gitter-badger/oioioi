import itertools
from django.db.models.signals import post_save
from django.dispatch import receiver
from oioioi.portals.models import Node
from oioioi.portals.widgets import REGISTERED_WIDGETS
from oioioi.problems.models import Problem


def parse_node_for_problems(node):
    """Parses node's content in search for links to problems
       and returns a queryset of problems accessible from the node.
    """
    widgets_with_links = itertools.ifilter(
                            lambda w: hasattr(w, 'get_problem_ids'),
                            REGISTERED_WIDGETS)

    ids = []
    for widget in widgets_with_links:
        regex = widget.compiled_tag_regex
        for m in regex.finditer(node.panel_code):
            ids += widget.get_problem_ids(m)

    return Problem.objects.filter(id__in=ids).distinct()


@receiver(post_save, sender=Node)
def update_task_information_cache(sender, instance, **kwargs):
    instance.problems_in_content = parse_node_for_problems(instance)
