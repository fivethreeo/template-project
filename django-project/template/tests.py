import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import runpy
import signal
import tempfile
from threading import Thread
from urllib.error import HTTPError, URLError
from unittest.mock import Mock, patch

from celery import Celery, shared_task
from celery.exceptions import Retry
from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.core.cache.backends.memcached import PyMemcacheCache
from django.template import TemplateSyntaxError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import get_script_prefix, reverse, set_script_prefix

from .celery import app
from .models import Job
from .tasks import notify


@shared_task
def runtime_test_task(value):
    return value.upper()


class OptionalFrontendTests(SimpleTestCase):
    @override_settings(FRONTEND_ENABLED=False, RSBUILD_MANIFEST='/missing/manifest.json')
    def test_django_page_renders_without_reading_a_disabled_frontend_manifest(self):
        with patch('template.templatetags.rsbuild._load_manifest') as load_manifest:
            response = self.client.get('/')
        self.assertContains(response, '<h1>Template Django</h1>', html=True)
        self.assertNotContains(response, '<script')
        self.assertNotContains(response, 'rel="stylesheet"')
        load_manifest.assert_not_called()

    @override_settings(FRONTEND_ENABLED=True, RSBUILD_MANIFEST='/missing/manifest.json')
    def test_enabled_frontend_still_requires_a_built_manifest(self):
        with self.assertRaisesMessage(TemplateSyntaxError, 'dev server or build first'):
            self.client.get('/')

    def test_enabled_frontend_still_emits_production_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / 'manifest.json'
            manifest.write_text(json.dumps({'entries': {'index': {'initial': {
                'js': ['/js/index.js'], 'css': ['/css/index.css'],
            }}}}))
            with override_settings(
                FRONTEND_ENABLED=True, RSBUILD_MANIFEST=manifest,
                RSBUILD_BASE_URL='/template/static/',
            ):
                response = self.client.get('/')
        self.assertContains(response, '<script defer src="/template/static/js/index.js"></script>', html=True)
        self.assertContains(response, '<link rel="stylesheet" href="/template/static/css/index.css">', html=True)


class RuntimeSettingsTests(SimpleTestCase):
    def configuration(self, environment=None, contents=''):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / '.env'
            env_file.write_text(contents)
            values = {'DJANGO_ENV_FILE': str(env_file), **(environment or {})}
            with patch.dict(os.environ, values, clear=True):
                return runpy.run_path(str(Path(__file__).with_name('local_settings.py')))

    def test_dotenv_configures_postgresql_and_preserves_literal_passwords(self):
        config = self.configuration(contents=(
            'DJANGO_DEBUG=false\n'
            'DJANGO_SECRET_KEY=test-only-key\n'
            'DJANGO_ALLOWED_HOSTS=example.com,192.0.2.1\n'
            'DJANGO_DATABASE_NAME=template\n'
            'DJANGO_DATABASE_USER=app_user\n'
            'DJANGO_DATABASE_PASSWORD="literal-${UNCHANGED}-password"\n'
            'DJANGO_DATABASE_HOST=database.example\n'
            'DJANGO_DATABASE_PORT=5432\n'
        ))
        self.assertFalse(config['DEBUG'])
        self.assertEqual(config['ALLOWED_HOSTS'], ['example.com', '192.0.2.1'])
        database = config['DATABASES']['default']
        self.assertEqual(database['ENGINE'], 'django.db.backends.postgresql')
        self.assertEqual(database['NAME'], 'template')
        self.assertEqual(database['PASSWORD'], 'literal-${UNCHANGED}-password')

    def test_process_environment_wins_over_dotenv(self):
        config = self.configuration(
            {
                'DJANGO_DEBUG': 'true',
                'DJANGO_SECRET_KEY': 'process-test-key',
                'DJANGO_INTERNAL_AUTH_TOKEN': 'process-test-token',
            },
            'DJANGO_DEBUG=false\nDJANGO_SECRET_KEY=file-test-key\n'
            'DJANGO_INTERNAL_AUTH_TOKEN=file-test-token\n',
        )
        self.assertTrue(config['DEBUG'])
        self.assertEqual(config['SECRET_KEY'], 'process-test-key')
        self.assertEqual(config['INTERNAL_AUTH_TOKEN'], 'process-test-token')

    def test_unconfigured_development_keeps_base_sqlite_database(self):
        self.assertNotIn('DATABASES', self.configuration())

    def test_frontend_defaults_to_directory_presence_and_can_be_overridden(self):
        for present in (False, True):
            with self.subTest(present=present), patch.object(Path, 'is_dir', return_value=present):
                config = self.configuration()
                self.assertEqual(config['FRONTEND_ENABLED'], present)
                if not present:
                    self.assertEqual(config['CORS_ALLOWED_ORIGINS'], [])
                for value, expected in [('false', False), ('true', True)]:
                    config = self.configuration({'DJANGO_FRONTEND_ENABLED': value})
                    self.assertEqual(config['FRONTEND_ENABLED'], expected)

    def test_development_cors_allows_loopback_and_configured_frontend_origin(self):
        config = self.configuration({
            'DJANGO_FRONTEND_ENABLED': 'true',
            'RSBUILD_BASE_URL': 'http://192.0.2.10:3300/assets/',
        })
        self.assertEqual(config['CORS_ALLOWED_ORIGINS'], [
            'http://localhost:3000', 'http://127.0.0.1:3000', 'http://192.0.2.10:3300',
        ])
        self.assertFalse(config['CORS_ALLOW_CREDENTIALS'])
        self.assertEqual(config['CSRF_TRUSTED_ORIGINS'], [])

    def test_cors_defaults_do_not_override_explicit_configuration_or_production(self):
        for value, expected in [
            ('https://frontend.example', ['https://frontend.example']), ('', []),
        ]:
            with self.subTest(value=value):
                config = self.configuration({'DJANGO_CORS_ALLOWED_ORIGINS': value})
                self.assertEqual(config['CORS_ALLOWED_ORIGINS'], expected)
        config = self.configuration({
            'DJANGO_DEBUG': 'false', 'DJANGO_SECRET_KEY': 'test-only-key',
            'DJANGO_DATABASE_NAME': 'production-database',
            'RSBUILD_BASE_URL': 'https://frontend.example/assets/',
        })
        self.assertEqual(config['CORS_ALLOWED_ORIGINS'], [])

    def test_development_sqlite_broker_is_separate_from_djangos_database(self):
        for contents in ['', 'CELERY_BROKER_URL=\n']:
            with self.subTest(contents=contents):
                config = self.configuration(contents=contents)
                self.assertNotIn('DATABASES', config)
                self.assertTrue(config['CELERY_BROKER_URL'].startswith('sqla+sqlite:///'))
                self.assertTrue(config['CELERY_BROKER_URL'].endswith('/celery-broker.sqlite3'))

    def test_production_and_explicit_brokers_do_not_switch_to_sqlite(self):
        config = self.configuration({
            'DJANGO_DEBUG': 'false', 'DJANGO_SECRET_KEY': 'test-only-key',
            'DJANGO_DATABASE_NAME': 'production-database',
        })
        self.assertEqual(config['CELERY_BROKER_URL'], 'amqp://localhost')
        config = self.configuration({'CELERY_BROKER_URL': 'amqp://dev-broker/dev'})
        self.assertEqual(config['CELERY_BROKER_URL'], 'amqp://dev-broker/dev')

    def test_memcached_is_opt_in(self):
        for environment in [{}, {'DJANGO_MEMCACHED_LOCATION': ''}]:
            with self.subTest(environment=environment):
                self.assertNotIn('CACHES', self.configuration(environment))

    def test_django_mount_prefix_configures_reversed_urls(self):
        self.assertIsNone(self.configuration()['FORCE_SCRIPT_NAME'])
        config = self.configuration({'DJANGO_SCRIPT_NAME': '/django'})
        self.assertEqual(config['FORCE_SCRIPT_NAME'], '/django')

    def test_backend_locations_load_from_env_in_dev_and_production(self):
        self.assertEqual(self.configuration()['TS_BACKEND_URL'], '')
        self.assertEqual(self.configuration()['TS_WS_BACKEND_URL'], '')
        for debug, http_url, ws_url in [
            ('true', 'http://localhost:8001/', 'ws://localhost:8002/'),
            ('false', '/v1/', '/socket/'),
        ]:
            with self.subTest(debug=debug):
                config = self.configuration(contents=(
                    f'DJANGO_DEBUG={debug}\n'
                    'DJANGO_SECRET_KEY=test-only-key\n'
                    'DJANGO_DATABASE_NAME=test-only-database\n'
                    f'DJANGO_TS_BACKEND_URL={http_url}\n'
                    f'DJANGO_TS_WS_BACKEND_URL={ws_url}\n'
                ))
                self.assertEqual(config['TS_BACKEND_URL'], http_url)
                self.assertEqual(config['TS_WS_BACKEND_URL'], ws_url)

    def test_memcached_uses_distinct_deployment_namespaces(self):
        keys = []
        for prefix in ['template', 'template_staging']:
            config = self.configuration(contents=(
                'DJANGO_MEMCACHED_LOCATION=127.0.0.1:11211\n'
                f'DJANGO_CACHE_KEY_PREFIX={prefix}\n'
            ))
            backend = config['CACHES']['default']
            self.assertEqual(
                backend['BACKEND'], 'django.core.cache.backends.memcached.PyMemcacheCache',
            )
            cache = PyMemcacheCache(backend['LOCATION'], backend)
            keys.append(cache.make_key('same-key'))
        self.assertNotEqual(*keys)

    def test_production_requires_secret_and_database(self):
        for environment in [
            {'DJANGO_DEBUG': 'false'},
            {'DJANGO_DEBUG': 'false', 'DJANGO_SECRET_KEY': ''},
            {'DJANGO_DEBUG': 'false', 'DJANGO_SECRET_KEY': 'test-only-key'},
        ]:
            with self.subTest(environment=environment):
                with self.assertRaises(ImproperlyConfigured):
                    self.configuration(environment)

    def test_legacy_database_variables_still_work(self):
        config = self.configuration({
            'DATABASE_NAME': 'legacy.sqlite3',
            'DATABASE_ENGINE': 'django.db.backends.sqlite3',
        })
        self.assertEqual(config['DATABASES']['default']['ENGINE'], 'django.db.backends.sqlite3')
        self.assertEqual(config['DATABASES']['default']['NAME'], 'legacy.sqlite3')

    def test_persistent_files_and_proxy_configuration(self):
        config = self.configuration({
            'DJANGO_DEBUG': 'false',
            'DJANGO_SECRET_KEY': 'test-only-key',
            'DJANGO_DATABASE_NAME': 'template',
            'DJANGO_STATIC_ROOT': '/srv/template/static',
            'DJANGO_MEDIA_ROOT': '/srv/template/media',
            'DJANGO_TRUST_PROXY_HEADERS': 'true',
        })
        self.assertEqual(config['RSBUILD_MANIFEST'], Path('/srv/template/static/manifest.json'))
        self.assertEqual(config['MEDIA_ROOT'], '/srv/template/media')
        self.assertEqual(config['FILE_UPLOAD_PERMISSIONS'], 0o640)
        self.assertEqual(config['FILE_UPLOAD_DIRECTORY_PERMISSIONS'], 0o2750)
        self.assertEqual(config['SECURE_PROXY_SSL_HEADER'], ('HTTP_X_FORWARDED_PROTO', 'https'))

    def test_subpath_deployment_isolates_assets_and_browser_cookies(self):
        config = self.configuration({
            'DJANGO_STATIC_URL': '/template/static/',
            'DJANGO_MEDIA_URL': '/template/media/',
            'DJANGO_SESSION_COOKIE_NAME': 'template_sessionid',
            'DJANGO_CSRF_COOKIE_NAME': 'template_csrftoken',
            'DJANGO_SESSION_COOKIE_PATH': '/template/',
            'DJANGO_CSRF_COOKIE_PATH': '/template/',
        })
        self.assertEqual(config['STATIC_URL'], '/template/static/')
        self.assertEqual(config['MEDIA_URL'], '/template/media/')
        self.assertEqual(config['SESSION_COOKIE_NAME'], 'template_sessionid')
        self.assertEqual(config['CSRF_COOKIE_NAME'], 'template_csrftoken')
        self.assertEqual(config['SESSION_COOKIE_PATH'], '/template/')
        self.assertEqual(config['CSRF_COOKIE_PATH'], '/template/')


class DevelopmentCorsTests(SimpleTestCase):
    @override_settings(CORS_ALLOWED_ORIGINS=['http://localhost:3000', 'http://127.0.0.1:3000'])
    def test_port_3000_receives_cors_headers_on_real_responses(self):
        for origin in ['http://localhost:3000', 'http://127.0.0.1:3000']:
            with self.subTest(origin=origin):
                response = self.client.get('/admin/login/', HTTP_ORIGIN=origin)
                self.assertEqual(response['Access-Control-Allow-Origin'], origin)
        response = self.client.get('/admin/login/', HTTP_ORIGIN='http://localhost:3001')
        self.assertNotIn('Access-Control-Allow-Origin', response)

    @override_settings(CORS_ALLOWED_ORIGINS=['http://localhost:3000'])
    def test_frontend_preflight_allows_post_and_csrf_header(self):
        response = self.client.options(
            '/', HTTP_ORIGIN='http://localhost:3000',
            HTTP_ACCESS_CONTROL_REQUEST_METHOD='POST',
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS='content-type,x-csrftoken',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Access-Control-Allow-Origin'], 'http://localhost:3000')
        self.assertIn('POST', response['Access-Control-Allow-Methods'])
        self.assertIn('x-csrftoken', response['Access-Control-Allow-Headers'])


class CeleryIntegrationTests(SimpleTestCase):
    @override_settings(
        CELERY_TASK_ALWAYS_EAGER=True,
        CELERY_TASK_EAGER_PROPAGATES=True,
        CELERY_BROKER_URL='memory://',
    )
    def test_shared_task_uses_django_celery_configuration(self):
        self.assertTrue(app.conf.task_always_eager)
        result = runtime_test_task.delay('template')
        self.assertEqual(result.get(), 'TEMPLATE')


@override_settings(
    WS_INTERNAL_URL='http://127.0.0.1:8002/',
    WS_INTERNAL_NOTIFY_TOKEN='test-only-notify',
)
class NotificationTaskTests(SimpleTestCase):
    def setUp(self):
        self.transport_patch = patch('template.tasks._opener.open')
        self.transport = self.transport_patch.start()
        self.addCleanup(self.transport_patch.stop)
        self.transport.return_value.__enter__.return_value.status = 202

    def test_chained_notification_authenticates_and_preserves_the_work_result(self):
        result = (runtime_test_task.s('done') | notify.s(42, 'work.finished')).apply().get()
        self.assertEqual(result, 'DONE')
        request = self.transport.call_args.args[0]
        self.assertEqual(request.full_url, 'http://127.0.0.1:8002/internal/notify/')
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(request.get_header('Authorization'), 'Bearer test-only-notify')
        self.assertEqual(request.get_header('Content-type'), 'application/json')
        self.assertEqual(json.loads(request.data), {
            'user_id': 42, 'event': 'work.finished', 'result': 'DONE',
        })
        self.assertEqual(self.transport.call_args.kwargs['timeout'], 5)

    def test_missing_sender_configuration_skips_delivery_and_warns_only_at_startup(self):
        for missing in ['WS_INTERNAL_URL', 'WS_INTERNAL_NOTIFY_TOKEN']:
            with self.subTest(missing=missing), override_settings(
                INTERNAL_AUTH_TOKEN='test-only-auth', **{missing: ''},
            ):
                with self.assertLogs('template.apps', level='WARNING') as logs:
                    apps.get_app_config('template').ready()
                    self.assertEqual(notify.run('done', 42), 'done')
                    self.assertEqual(notify.run('done', 42), 'done')
                self.assertEqual(len(logs.output), 1)
                self.assertIn('Celery WebSocket notifications are disabled', logs.output[0])
        self.transport.assert_not_called()

    def test_transient_network_and_http_failures_retry(self):
        errors = [URLError('unavailable'), TimeoutError('timed out')]
        errors += [
            HTTPError('http://127.0.0.1:8002/internal/notify/', code, 'temporary', {}, io.BytesIO())
            for code in [429, 500, 503]
        ]
        for error in errors:
            with self.subTest(error=error):
                self.transport.side_effect = error
                with patch.object(notify, 'retry', side_effect=Retry()) as retry:
                    with self.assertRaises(Retry):
                        notify.run('done', 42)
                retry.assert_called_once_with(exc=error, countdown=1)

    def test_permanent_http_failures_do_not_retry(self):
        for code in [400, 403, 404, 413]:
            error = HTTPError(
                'http://127.0.0.1:8002/internal/notify/', code, 'rejected', {}, io.BytesIO(),
            )
            with self.subTest(code=code), patch.object(notify, 'retry') as retry:
                self.transport.side_effect = error
                with self.assertRaises(HTTPError):
                    notify.run('done', 42)
                retry.assert_not_called()

    def test_invalid_or_oversized_notifications_are_rejected_before_delivery(self):
        for result, user_id, event in [
            ('done', 0, 'work.finished'), ('done', True, 'work.finished'),
            ('done', 2**53, 'work.finished'), ('done', '', 'work.finished'),
            ('done', 42, ''), ('x' * 65536, 42, 'work.finished'),
            (float('nan'), 42, 'work.finished'),
        ]:
            with self.subTest(user_id=user_id, event=event), self.assertRaises(ValueError):
                notify.run(result, user_id, event)
        self.transport.assert_not_called()

    def test_unexpected_success_response_is_not_accepted(self):
        self.transport.return_value.__enter__.return_value.status = 200
        with patch.object(notify, 'retry') as retry, self.assertRaises(ValueError):
            notify.run('done', 42)
        retry.assert_not_called()

    def test_redirects_are_not_followed_with_service_credentials(self):
        self.transport_patch.stop()
        requests = []

        class RedirectHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append(self.path)
                self.rfile.read(int(self.headers.get('Content-Length', 0)))
                self.send_response(302)
                self.send_header('Location', '/redirect-target')
                self.send_header('Content-Length', '0')
                self.end_headers()

            def do_GET(self):
                requests.append(self.path)
                self.send_response(202)
                self.end_headers()

            def log_message(self, format, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), RedirectHandler)
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            with override_settings(WS_INTERNAL_URL=f'http://127.0.0.1:{server.server_port}/'):
                with self.assertRaises(HTTPError) as error:
                    notify.run('done', 42)
                self.assertEqual(error.exception.code, 302)
            self.assertEqual(requests, ['/internal/notify/'])
        finally:
            server.shutdown()
            thread.join()
            server.server_close()


class DjangoMountTests(SimpleTestCase):
    @override_settings(FORCE_SCRIPT_NAME='/django')
    def test_homepage_and_admin_respect_the_mount_prefix(self):
        prefix = get_script_prefix()
        self.addCleanup(set_script_prefix, prefix)
        set_script_prefix('/django/')
        response = self.client.get('/')
        self.assertContains(response, '<div id="root"></div>', html=True)
        response = self.client.get('/admin/login/')
        self.assertContains(response, 'action="/django/admin/login/"')

    def test_body_exposes_backend_urls_in_dev_prod_and_disabled_cases(self):
        for http_url, ws_url in [
            ('http://localhost:8001/', 'ws://localhost:8002/'),
            ('/api/', '/ws/'),
            ('', ''),
        ]:
            with self.subTest(http_url=http_url), override_settings(
                TS_BACKEND_URL=http_url, TS_WS_BACKEND_URL=ws_url,
            ):
                response = self.client.get('/')
                self.assertContains(
                    response,
                    f'<body data-ts-backend-url="{http_url}" data-ts-ws-backend-url="{ws_url}">',
                )


class JobResultsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(username='job-owner')
        cls.other_user = get_user_model().objects.create_user(username='other-job-owner')
        cls.job = Job.objects.create(user=cls.user, task_id='owned-task')

    def setUp(self):
        self.client.force_login(self.user)
        self.url = reverse('job-results')
        self.application = Mock()
        self.application.conf.result_backend = 'cache+memory://'
        self.application.backend.get_task_meta.return_value = {'status': 'SUCCESS', 'result': 5}
        application_patch = patch('template.views.app', self.application)
        application_patch.start()
        self.addCleanup(application_patch.stop)

    def test_requires_a_user_session_and_get_without_querying_results(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 401)
        self.client.force_login(self.user)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response['Allow'], 'GET')
        self.application.backend.get_task_meta.assert_not_called()

    def test_returns_only_owned_job_statuses_and_successful_results(self):
        Job.objects.create(user=self.user, task_id='failed-task')
        Job.objects.create(user=self.user, task_id='pending-task')
        Job.objects.create(user=self.other_user, task_id='private-task')
        metadata = {
            'owned-task': {'status': 'SUCCESS', 'result': {'total': 5}},
            'failed-task': {
                'status': 'FAILURE',
                'result': ValueError('test-only-private-error'),
                'traceback': 'test-only-private-traceback',
            },
            'pending-task': {'status': 'PENDING', 'result': None},
        }
        self.application.backend.get_task_meta.side_effect = lambda task_id, **kwargs: metadata[task_id]
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertIn('no-store', response['Cache-Control'])
        data = response.json()
        self.assertEqual(data['count'], 3)
        jobs = {job['task_id']: job for job in data['results']}
        self.assertEqual(set(jobs), set(metadata))
        self.assertEqual(jobs['owned-task']['result'], {'total': 5})
        self.assertEqual(jobs['failed-task']['status'], 'FAILURE')
        self.assertIsNone(jobs['failed-task']['result'])
        self.assertIsNone(jobs['pending-task']['result'])
        self.assertNotIn('test-only-private', response.content.decode())

    def test_paginates_owned_jobs_and_rejects_invalid_pages_without_result_queries(self):
        Job.objects.bulk_create([
            Job(user=self.user, task_id=f'owned-task-{index}') for index in range(25)
        ] + [
            Job(user=self.other_user, task_id=f'private-task-{index}') for index in range(25)
        ])
        expected = list(Job.objects.filter(user=self.user).values_list('task_id', flat=True))
        first = self.client.get(self.url).json()
        self.assertEqual((first['count'], first['page'], first['pages']), (26, 1, 2))
        self.assertEqual([job['task_id'] for job in first['results']], expected[:25])
        self.assertEqual(self.application.backend.get_task_meta.call_count, 25)
        second = self.client.get(self.url, {'page': 2}).json()
        self.assertEqual([job['task_id'] for job in second['results']], expected[25:])
        self.assertEqual(self.application.backend.get_task_meta.call_count, 26)
        self.application.backend.get_task_meta.reset_mock()
        for page, status in [('invalid', 400), ('0', 404), ('3', 404)]:
            with self.subTest(page=page):
                self.assertEqual(self.client.get(self.url, {'page': page}).status_code, status)
        self.application.backend.get_task_meta.assert_not_called()

    def test_unconfigured_storage_returns_503_but_empty_job_lists_still_work(self):
        self.application.conf.result_backend = None
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 503)
        self.assertIn('no-store', response['Cache-Control'])
        self.application.backend.get_task_meta.assert_not_called()
        self.job.delete()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['results'], [])

    def test_backend_errors_do_not_expose_exception_details(self):
        self.application.backend.get_task_meta.side_effect = ConnectionError('test-only-private-error')
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('test-only-private-error', response.content.decode())

    def test_reads_results_persisted_in_a_real_sqlite_celery_backend(self):
        with tempfile.TemporaryDirectory() as directory:
            result_app = Celery(
                'job-results-test',
                backend=f'db+sqlite:///{Path(directory) / "results.sqlite3"}',
                set_as_current=False,
            )
            try:
                result_app.backend.store_result(self.job.task_id, {'total': 5}, 'SUCCESS')
                with patch('template.views.app', result_app):
                    response = self.client.get(self.url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()['results'][0]['result'], {'total': 5})
            finally:
                result_app.close()

    def test_non_json_results_return_a_controlled_error(self):
        self.application.backend.get_task_meta.return_value = {'status': 'SUCCESS', 'result': b'bytes'}
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {'error': 'Unable to encode task results'})


@override_settings(
    INTERNAL_AUTH_TOKEN='test-only-service-token',
    WS_INTERNAL_URL='http://127.0.0.1:8002/',
    WS_INTERNAL_NOTIFY_TOKEN='test-only-notify',
)
class InternalAuthenticationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(username='auth-user')

    def setUp(self):
        self.url = reverse('auth')
        self.headers = {'HTTP_AUTHORIZATION': 'Bearer test-only-service-token'}

    def test_valid_service_and_session_return_only_user_id_without_caching(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url, **self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'user_id': self.user.pk})
        self.assertIn('no-store', response['Cache-Control'])

    def test_valid_session_does_not_bypass_service_authentication(self):
        self.client.force_login(self.user)
        for authorization in ['', 'Bearer wrong-token', 'Basic test-only-service-token', 'Bearer \u00e9']:
            with self.subTest(authorization=authorization):
                response = self.client.get(self.url, HTTP_AUTHORIZATION=authorization)
                self.assertEqual(response.status_code, 403)
                self.assertIn('no-store', response['Cache-Control'])

    @override_settings(INTERNAL_AUTH_TOKEN='')
    def test_unconfigured_endpoint_is_disabled_even_with_a_valid_session(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url, HTTP_AUTHORIZATION='Bearer ')
        self.assertEqual(response.status_code, 503)
        self.assertIn('no-store', response['Cache-Control'])

    def test_service_token_without_a_valid_session_is_rejected(self):
        for cookie in [None, 'invalid-session']:
            with self.subTest(cookie=cookie):
                self.client.cookies.clear()
                if cookie:
                    self.client.cookies[settings.SESSION_COOKIE_NAME] = cookie
                response = self.client.get(self.url, **self.headers)
                self.assertEqual(response.status_code, 401)
                self.assertIn('no-store', response['Cache-Control'])

    def test_logged_out_session_cookie_cannot_be_replayed(self):
        self.client.force_login(self.user)
        session_cookie = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        self.client.logout()
        self.client.cookies[settings.SESSION_COOKIE_NAME] = session_cookie
        response = self.client.get(self.url, **self.headers)
        self.assertEqual(response.status_code, 401)

    def test_non_get_requests_are_rejected(self):
        self.client.force_login(self.user)
        response = self.client.post(self.url, **self.headers)
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response['Allow'], 'GET')
        self.assertIn('no-store', response['Cache-Control'])

    @override_settings(INTERNAL_AUTH_TOKEN='')
    def test_missing_token_warns_at_startup_not_per_request(self):
        with self.assertLogs('template.apps', level='WARNING') as logs:
            apps.get_app_config('template').ready()
            for _ in range(2):
                self.assertEqual(self.client.get(self.url).status_code, 503)
        self.assertEqual(len(logs.output), 1)
        self.assertIn('DJANGO_INTERNAL_AUTH_TOKEN is not set', logs.output[0])

    def test_configured_token_does_not_warn_at_startup(self):
        with self.assertNoLogs('template.apps', level='WARNING'):
            apps.get_app_config('template').ready()

    @override_settings(
        SESSION_COOKIE_NAME='template_sessionid', SESSION_COOKIE_PATH='/template/',
        CSRF_COOKIE_NAME='template_csrftoken', CSRF_COOKIE_PATH='/template/',
        STATIC_URL='/template/static/',
    )
    def test_named_session_and_admin_cookie_do_not_use_the_other_apps_cookie(self):
        self.client.force_login(self.user)
        self.client.cookies['sessionid'] = 'unrelated-prosango-session'
        response = self.client.get(self.url, **self.headers)
        self.assertEqual(response.json(), {'user_id': self.user.pk})
        response = self.client.get('/admin/login/')
        self.assertContains(response, '/template/static/admin/css/base.css')
        self.assertIn('template_csrftoken', response.cookies)
        self.assertEqual(response.cookies['template_csrftoken']['path'], '/template/')
        self.assertNotIn('csrftoken', response.cookies)


class FrontendReloadTests(SimpleTestCase):
    def configure_reload(self):
        return runpy.run_path(str(Path(__file__).resolve().parents[1] / 'manage.py'))['configure_frontend_reload']

    def test_dev_hup_touches_a_file_watched_by_the_django_autoreloader(self):
        with tempfile.TemporaryDirectory() as directory:
            trigger = Path(directory) / 'reload'
            with (
                patch.dict(os.environ, {'DJANGO_RELOAD_TRIGGER': str(trigger)}),
                patch('signal.signal') as install,
                patch('django.utils.autoreload.autoreload_started.connect') as connect,
            ):
                self.configure_reload()()
            self.assertTrue(trigger.exists())
            self.assertEqual(install.call_args.args[0], signal.SIGHUP)
            reloader = Mock(extra_files=set())
            connect.call_args.args[0](sender=reloader)
            self.assertIn(trigger, reloader.extra_files)
            trigger.unlink()
            install.call_args.args[1](signal.SIGHUP, None)
            self.assertTrue(trigger.exists())

    def test_normal_django_commands_do_not_install_a_hup_handler(self):
        with patch.dict(os.environ, {'DJANGO_RELOAD_TRIGGER': ''}), patch('signal.signal') as install:
            self.configure_reload()()
        install.assert_not_called()
