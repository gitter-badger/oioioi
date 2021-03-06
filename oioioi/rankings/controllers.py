from collections import defaultdict
from operator import itemgetter
import unicodecsv

from django.conf import settings
from django.core.urlresolvers import reverse
from django.http import HttpResponse
from django.template import RequestContext
from django.template.loader import render_to_string
from django.utils.encoding import force_unicode
from django.utils.translation import ugettext_lazy as _
from django.contrib.auth.models import User

from oioioi.base.utils import RegisteredSubclassesBase, ObjectWithMixins
from oioioi.base.utils import group_cache
from oioioi.contests.models import ProblemInstance, UserResultForProblem, \
        Submission
from oioioi.contests.controllers import ContestController
from oioioi.contests.utils import is_contest_admin, is_contest_observer, \
        contest_exists, can_enter_contest
from oioioi.filetracker.utils import make_content_disposition_header

from oioioi.base.utils.cache_generator import CacheGenerator


CONTEST_RANKING_KEY = 'c'


class RankingMixinForContestController(object):
    def ranking_controller(self):
        """Return the actual :class:`RankingController` for the contest."""
        return DefaultRankingController(self.contest)

    def update_user_results(self, user, problem_instance, *args, **kwargs):
        super(RankingMixinForContestController, self) \
            .update_user_results(user, problem_instance, *args, **kwargs)
        contest_id = problem_instance.round.contest.id
        ranking_cache_group = self.ranking_controller() \
            .get_cache_group(contest_id)
        group_cache.invalidate(ranking_cache_group)

ContestController.mix_in(RankingMixinForContestController)


class RankingController(RegisteredSubclassesBase, ObjectWithMixins):
    modules_with_subclasses = ['controllers']
    abstract = True

    def __init__(self, contest):
        self.contest = contest

    def available_rankings(self, request):
        """Returns a list of available rankings.

           Each ranking is a pair ``(key, description)``.
        """
        raise NotImplementedError

    def can_search_for_users(self):
        """Determines if in this ranking, searching for users is enabled."""
        return False

    def find_user_position(self, user, request, key):
        """Returns user's position in the ranking.
           User should be an object of class User, not a string with username.

           If user is not in the ranking, None is returned.
        """
        raise NotImplementedError

    def get_rendered_ranking(self, request, key):
        """Wraps :meth:`render_ranking` method, caching its results."""
        ranking_cache_group = self.get_cache_group(self.contest.id)
        ranking_cache_key = self.get_cache_key(request, key,
                                               request.GET.get('page', 1))

        with CacheGenerator(ranking_cache_key, ranking_cache_group) as cg:
            f = (lambda: self.render_ranking(request, key))
            html = cg.get_cached_obj(f, settings.RANKING_CACHE_TIMEOUT)

        return html

    def render_ranking(self, request, key):
        raise NotImplementedError

    def render_ranking_to_csv(self, request, key):
        raise NotImplementedError

    def get_serialized_ranking(self, request, key):
        """Wraps :meth:`serialize_ranking` method, caching its results."""
        ranking_cache_group = self.get_cache_group(self.contest.id)
        ranking_cache_key = self.get_cache_key(request, key)

        with CacheGenerator(ranking_cache_key, ranking_cache_group) as cg:
            generate_data = (lambda: self.serialize_ranking(request, key))
            ranking_data = cg.get_cached_obj(generate_data,
                                             settings.RANKING_CACHE_TIMEOUT)
        return ranking_data

    def serialize_ranking(self, request, key):
        """Returns some data (representing ranking).
           This data will be used by :meth:`render_ranking`
           to generate the html code.
        """
        raise NotImplementedError

    def get_cache_group(self, contest_id):
        """Returns a group key to be used with group_cache."""
        return 'ranking_%s' % str(contest_id)

    def get_cache_key(self, request, key, page=None):
        """Returns a cache key to be used with group_cache.

           When caching is enabled, every ranking is cached under a
           group corresponding to its contest. All cached rankings in a
           contest are invalidated when a submission is judged.

           The cache key for each ranking should be constructed in a
           way that allows to distinguish between its different
           versions.

           For example, you may want to display a live version of the
           ranking to the admins. Users on the other hand should see
           results only from the finished rounds.

           Default implementation takes into account a few basic
           permissions. It is suitable for the above example and many
           more typical scenarios. However, if the way you generate and
           display the ranking differs significantly from the default
           implementation, you should carefully inspect the code below
           and modify it as needed.

           The html code of each page of the ranking is cached separately.
           Also, the whole ranking is cached as an object.
           :param page specifies the page to be cached.
           If page=None then this method returns the cache_key for whole
           ranking.
        """

        if page is not None:
            try:
                page = int(page)
            except ValueError:
                page = 1

        cache_key = ':'.join([key, str(request.user.is_superuser),
                              str(request.user.is_authenticated()),
                              str(is_contest_admin(request)),
                              str(is_contest_observer(request)),
                              str(page)])
        return cache_key


class DefaultRankingController(RankingController):
    description = _("Default ranking")

    def _rounds_for_ranking(self, request, key=CONTEST_RANKING_KEY):
        can_see_all = is_contest_admin(request) or is_contest_observer(request)
        ccontroller = self.contest.controller
        queryset = self.contest.round_set.all()
        if not ccontroller.can_see_ranking(request):
            return
        if key != CONTEST_RANKING_KEY:
            queryset = queryset.filter(id=key)
        for round in queryset:
            times = ccontroller.get_round_times(request, round)
            if can_see_all or times.public_results_visible(request.timestamp):
                yield round

    def available_rankings(self, request):
        rankings = [(CONTEST_RANKING_KEY, _("Contest"))]
        for round in self._rounds_for_ranking(request):
            rankings.append((str(round.id), round.name))
        if len(rankings) == 1:
            # No rounds have visible results
            return []
        if len(rankings) == 2:
            # Only a single round => call this "contest ranking".
            return rankings[:1]
        return rankings

    def can_search_for_users(self):
        return True

    def find_user_position(self, user, request, key):
        data = self.get_serialized_ranking(request, key)
        rows = data['rows']
        for i, row in enumerate(rows):
            if row['user'] == user:
                return i + 1
        # User not found
        return None

    def render_ranking(self, request, key):
        data = self.get_serialized_ranking(request, key)
        return render_to_string('rankings/default_ranking.html',
                context_instance=RequestContext(request, data))

    def _get_csv_header(self, request, data):
        header = [_("No."), _("First name"), _("Last name")]
        for pi, _statement_visible in data['problem_instances']:
            header.append(pi.get_short_name_display())
        header.append(_("Sum"))
        return header

    def _get_csv_row(self, request, row):
        line = [row['place'], row['user'].first_name, row['user'].last_name]
        line += [r.score if r and r.score is not None else ''
                 for r in row['results']]
        line.append(row['sum'])
        return line

    def render_ranking_to_csv(self, request, key):
        data = self.serialize_ranking(request, key)

        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = \
                make_content_disposition_header('attachment',
                    u'%s-%s-%s.csv' % (_("ranking"), request.contest.id, key))
        writer = unicodecsv.writer(response)

        writer.writerow(map(force_unicode,
                        self._get_csv_header(request, data)))
        for row in data['rows']:
            writer.writerow(map(force_unicode,
                            self._get_csv_row(request, row)))

        return response

    def filter_users_for_ranking(self, request, key, queryset):
        return queryset.filter(is_superuser=False)

    def _filter_pis_for_ranking(self, key, queryset):
        return queryset

    def _allow_zero_score(self):
        return True

    def _get_users_results(self, request, pis, results, rounds, users):
        contest = request.contest
        controller = contest.controller
        by_user = defaultdict(dict)
        for r in results:
            by_user[r.user_id][r.problem_instance_id] = r
        users = users.filter(id__in=by_user.keys())
        data = []
        all_rounds_trial = all(r.is_trial for r in rounds)
        for user in users.order_by('last_name', 'first_name', 'username'):
            by_user_row = by_user[user.id]
            user_results = []
            user_data = {
                'user': user,
                'results': user_results,
                'sum': None
            }

            submissions = Submission.objects.filter(
                    problem_instance__contest=contest)
            if is_contest_admin(request) or is_contest_observer(request):
                my_visible = submissions
            else:
                my_visible = controller.filter_my_visible_submissions(request,
                        submissions)

            for pi in pis:
                result = by_user_row.get(pi.id)
                if result and hasattr(result, 'submission_report') and \
                        hasattr(result.submission_report, 'submission'):
                    submission = result.submission_report.submission
                    kwargs = {'contest_id': contest.id,
                              'submission_id': submission.id}
                    if my_visible.filter(id=submission.id).exists():
                        result.url = reverse('submission', kwargs=kwargs)
                    elif controller.can_see_source(request, submission):
                        result.url = reverse('show_submission_source',
                                kwargs=kwargs)

                user_results.append(result)
                if result and result.score and \
                        (not pi.round.is_trial or all_rounds_trial):
                    if user_data['sum'] is None:
                        user_data['sum'] = result.score
                    else:
                        user_data['sum'] += result.score
            if user_data['sum'] is not None:
                # This rare corner case with sum being None may happen if all
                # user's submissions do not have scores (for example the
                # problems do not support scoring, or all the evaluations
                # failed with System Errors).
                if self._allow_zero_score() or user_data['sum'] != 0:
                    data.append(user_data)
        return data

    def _assign_places(self, data, extractor):
        """Assigns places to the serialized ranking ``data``.

           Extractor should return values by which users should be ordered in
           the ranking. Users with the same place should have same value
           returned.
        """
        data.sort(key=extractor, reverse=True)
        prev_sum = None
        place = None
        for i, row in enumerate(data, 1):
            if extractor(row) != prev_sum:
                place = i
                prev_sum = extractor(row)
            row['place'] = place

    def _is_problem_statement_visible(self, request, pi):
        controller = request.contest.controller
        return contest_exists(request) and \
               can_enter_contest(request) and \
               controller.can_see_problem(request, pi) and \
               controller.can_see_statement(request, pi)

    def _get_pis_with_visibility(self, request, pis):
        return [(pi, self._is_problem_statement_visible(request, pi))
                for pi in pis]

    def serialize_ranking(self, request, key):
        rounds = list(self._rounds_for_ranking(request, key))
        pis = list(self._filter_pis_for_ranking(key,
            ProblemInstance.objects.filter(round__in=rounds)).
            select_related('problem').prefetch_related('round'))
        users = self.filter_users_for_ranking(request, key, User.objects.all())
        results = UserResultForProblem.objects \
                .filter(problem_instance__in=pis, user__in=users) \
                .prefetch_related('problem_instance__round') \
                .select_related('submission_report', 'problem_instance',
                        'problem_instance__contest')

        data = self._get_users_results(request, pis, results, rounds, users)
        self._assign_places(data, itemgetter('sum'))
        return {'rows': data,
                'problem_instances': self._get_pis_with_visibility(request,
                                                                   pis),
                'participants_on_page': getattr(settings,
                    'PARTICIPANTS_ON_PAGE', 100)}
