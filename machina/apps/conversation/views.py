# -*- coding: utf-8 -*-

# Standard library imports
# Third party imports
from django.contrib import messages
from django.core.exceptions import ObjectDoesNotExist
from django.core.urlresolvers import reverse
from django.db.models import get_model
from django.forms.forms import NON_FIELD_ERRORS
from django.shortcuts import get_object_or_404
from django.utils.translation import ugettext_lazy as _
from django.views.generic import CreateView
from django.views.generic import ListView
from django.views.generic import UpdateView

# Local application / specific library imports
from machina.apps.conversation.signals import topic_viewed
from machina.apps.conversation.utils import get_client_ip
from machina.conf import settings as machina_settings
from machina.core.loading import get_class
from machina.views.mixins import PermissionRequiredMixin

Forum = get_model('forum', 'Forum')
Post = get_model('conversation', 'Post')
Topic = get_model('conversation', 'Topic')
TopicPollOption = get_model('polls', 'TopicPollOption')

PostForm = get_class('conversation.forms', 'PostForm')
TopicForm = get_class('conversation.forms', 'TopicForm')
TopicPollOptionFormset = get_class('polls.forms', 'TopicPollOptionFormset')
TopicPollVoteForm = get_class('polls.forms', 'TopicPollVoteForm')

PermissionHandler = get_class('permission.handler', 'PermissionHandler')
perm_handler = PermissionHandler()


class TopicView(PermissionRequiredMixin, ListView):
    template_name = 'conversation/topic_detail.html'
    context_object_name = 'posts'
    permission_required = ['can_read_forum', ]
    paginate_by = machina_settings.TOPIC_POSTS_NUMBER_PER_PAGE
    view_signal = topic_viewed

    poll_form_class = TopicPollVoteForm

    def get(self, request, **kwargs):
        topic = self.get_topic()

        # Handle pagination
        requested_post = request.GET.get('post', None)
        if requested_post:
            try:
                assert requested_post.isdigit()
                post = topic.posts.get(pk=requested_post)
                requested_page = (post.position // machina_settings.TOPIC_POSTS_NUMBER_PER_PAGE) + 1
                request.GET = request.GET.copy()  # A QueryDict is immutable
                request.GET.update({'page': requested_page})
            except (Post.DoesNotExist, AssertionError):
                pass

        response = super(TopicView, self).get(request, **kwargs)
        self.send_signal(request, response, topic)
        return response

    def get_topic(self):
        if not hasattr(self, 'topic'):
            self.topic = get_object_or_404(Topic, pk=self.kwargs['pk'])
        return self.topic

    def get_queryset(self):
        self.topic = self.get_topic()
        qs = self.topic.posts.all()
        return qs

    def get_controlled_object(self):
        """
        Returns the forum associated with the current topic in order to allow permission checks.
        """
        return self.get_topic().forum

    def get_context_data(self, **kwargs):
        context = super(TopicView, self).get_context_data(**kwargs)

        # Insert the considered topic and the associated forum into the context
        topic = self.get_topic()
        context['topic'] = topic
        context['forum'] = topic.forum

        # Handles the case when a poll is associated to the topic
        try:
            if hasattr(topic, 'poll'):
                context['poll'] = topic.poll
                context['poll_form'] = self.poll_form_class(poll=topic.poll)
                context['view_results_action'] = self.request.GET.get('view_results', None)
        except ObjectDoesNotExist:
            pass

        return context

    def send_signal(self, request, response, topic):
        self.view_signal.send(
            sender=self, topic=topic, user=request.user,
            request=request, response=response)


class PostEditMixin(object):
    success_message = _('This message has been posted successfully.')

    def get_form_kwargs(self):
        kwargs = super(PostEditMixin, self).get_form_kwargs()
        kwargs['user'] = self.request.user
        kwargs['user_ip'] = get_client_ip(self.request)
        kwargs['forum'] = self.get_forum()
        return kwargs

    def get_context_data(self, **kwargs):
        context = super(PostEditMixin, self).get_context_data(**kwargs)
        if hasattr(self, 'preview'):
            context['preview'] = self.preview
        # Insert the considered forum into the context
        context['forum'] = self.get_forum()
        return context

    def form_valid(self, form):
        if 'preview' in self.request.POST:
            self.preview = True
            return self.render_to_response(self.get_context_data(form=form))
        messages.success(self.request, self.success_message)
        return super(PostEditMixin, self).form_valid(form)

    def get_controlled_object(self):
        """
        Returns the forum associated with the topic being created.
        """
        return self.get_forum()

    def get_forum(self):
        if not hasattr(self, 'forum'):
            self.forum = get_object_or_404(Forum, pk=self.kwargs['forum_pk'])
        return self.forum


class TopicEditMixin(PostEditMixin):
    poll_option_formset_class = TopicPollOptionFormset

    def get_context_data(self, **kwargs):
        context = super(TopicEditMixin, self).get_context_data(**kwargs)

        topic = self.object.topic if self.object is not None else None
        poll_option_queryset = TopicPollOption.objects.filter(poll__topic=topic)

        if perm_handler.can_create_polls(self.get_forum(), self.request.user):
            # Add the poll option formset to the context
            if self.request.method == 'POST':
                context['poll_option_formset'] = self.poll_option_formset_class(
                    data=self.request.POST, topic=topic)
            else:
                context['poll_option_formset'] = self.poll_option_formset_class(
                    queryset=poll_option_queryset, topic=topic)

            # Handles the preview of the poll
            if hasattr(self, 'poll_preview'):
                context['poll_preview'] = self.poll_preview

        return context

    def form_valid(self, form):
        preview = 'preview' in self.request.POST
        save_poll_option_formset = False

        if perm_handler.can_create_polls(self.get_forum(), self.request.user):
            if len(form.cleaned_data['poll_question']):
                poll_option_formset = self.poll_option_formset_class(data=self.request.POST)
                if poll_option_formset.is_valid():
                    save_poll_option_formset = not preview
                    self.poll_preview = True
                else:
                    save_poll_option_formset = False
                    errors = list()
                    for error in poll_option_formset.errors:
                        if error:
                            errors += [v[0] for _, v in error.items()]
                    if not len(errors) and poll_option_formset._non_form_errors:
                        form._errors[NON_FIELD_ERRORS] = poll_option_formset._non_form_errors
                        messages.error(self.request, form._errors[NON_FIELD_ERRORS])
                    return self.form_invalid(form)

        valid = super(TopicEditMixin, self).form_valid(form)

        if save_poll_option_formset:
            poll_option_formset.topic = self.object.topic
            poll_option_formset.save(
                poll_question=form.cleaned_data.pop('poll_question', None),
                poll_max_options=form.cleaned_data.pop('poll_max_options', None),
                poll_duration=form.cleaned_data.pop('poll_duration', None),
                poll_user_changes=form.cleaned_data.pop('poll_user_changes', None),
            )

        return valid


class TopicCreateView(PermissionRequiredMixin, TopicEditMixin, CreateView):
    template_name = 'conversation/topic_create.html'
    permission_required = ['can_start_new_topics', ]
    form_class = TopicForm

    poll_option_formset_class = TopicPollOptionFormset

    def get_form_kwargs(self):
        kwargs = super(TopicCreateView, self).get_form_kwargs()
        kwargs['forum'] = self.get_forum()
        return kwargs

    def get_success_url(self):
        return reverse('conversation:topic', kwargs={
            'forum_pk': self.get_forum().pk,
            'pk': self.object.topic.pk})


class TopicUpdateView(PermissionRequiredMixin, TopicEditMixin, UpdateView):
    success_message = _('This message has been edited successfully.')
    template_name = 'conversation/topic_update.html'
    permission_required = []  # Defined in the 'perform_permissions_check()' method
    form_class = TopicForm
    model = Topic

    def get_object(self, queryset=None):
        topic = super(TopicUpdateView, self).get_object(queryset)
        return topic.first_post

    def get_form_kwargs(self):
        kwargs = super(TopicUpdateView, self).get_form_kwargs()
        kwargs['forum'] = self.get_forum()
        return kwargs

    def get_success_url(self):
        return reverse('conversation:topic', kwargs={
            'forum_pk': self.get_forum().pk,
            'pk': self.object.topic.pk})

    # Permissions checks

    def get_controlled_object(self):
        """
        Returns the post that will be edited.
        """
        return self.get_object()

    def perform_permissions_check(self, user, obj, perms):
        return perm_handler.can_edit_post(obj, user)


class PostCreateView(PermissionRequiredMixin, PostEditMixin, CreateView):
    template_name = 'conversation/post_create.html'
    permission_required = ['can_reply_to_topics', ]
    form_class = PostForm

    def get_form_kwargs(self):
        kwargs = super(PostCreateView, self).get_form_kwargs()
        kwargs['topic'] = self.get_topic()
        return kwargs

    def get_context_data(self, **kwargs):
        context = super(PostCreateView, self).get_context_data(**kwargs)
        # Insert the considered topic into the context
        context['topic'] = self.get_topic()

        return context

    def get_success_url(self):
        return '{0}?post={1}#{1}'.format(
            reverse('conversation:topic', kwargs={
                'forum_pk': self.get_forum().pk,
                'pk': self.object.topic.pk}),
            self.object.pk)

    def get_topic(self):
        if not hasattr(self, 'topic'):
            self.topic = get_object_or_404(Topic, pk=self.kwargs['topic_pk'])
        return self.topic


class PostUpdateView(PermissionRequiredMixin, PostEditMixin, UpdateView):
    success_message = _('This message has been edited successfully.')
    template_name = 'conversation/post_update.html'
    permission_required = []  # Defined in the 'perform_permissions_check()' method
    form_class = PostForm
    model = Post

    def get_form_kwargs(self):
        kwargs = super(PostUpdateView, self).get_form_kwargs()
        kwargs['topic'] = self.get_topic()
        return kwargs

    def get_context_data(self, **kwargs):
        context = super(PostUpdateView, self).get_context_data(**kwargs)
        # Insert the considered topic into the context
        context['topic'] = self.get_topic()

        return context

    def get_success_url(self):
        return '{0}?post={1}#{1}'.format(
            reverse('conversation:topic', kwargs={
                'forum_pk': self.get_forum().pk,
                'pk': self.object.topic.pk}),
            self.object.pk)

    def get_topic(self):
        if not hasattr(self, 'topic'):
            self.topic = get_object_or_404(Topic, pk=self.kwargs['topic_pk'])
        return self.topic

    # Permissions checks

    def get_controlled_object(self):
        """
        Returns the post that will be edited.
        """
        return self.get_object()

    def perform_permissions_check(self, user, obj, perms):
        return perm_handler.can_edit_post(obj, user)
