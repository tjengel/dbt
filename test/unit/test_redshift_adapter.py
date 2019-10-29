import unittest
from unittest import mock

import dbt.adapters
import dbt.flags as flags
import dbt.utils

from dbt.adapters.redshift import RedshiftAdapter
from dbt.exceptions import FailedToConnectException
from dbt.logger import GLOBAL_LOGGER as logger  # noqa

from .utils import config_from_parts_or_dicts, mock_connection


@classmethod
def fetch_cluster_credentials(*args, **kwargs):
    return {
        'DbUser': 'root',
        'DbPassword': 'tmp_password'
    }


class TestRedshiftAdapter(unittest.TestCase):

    def setUp(self):
        flags.STRICT_MODE = True

        profile_cfg = {
            'config': {
                'query_comment': 'dbt'
            },
            'outputs': {
                'test': {
                    'type': 'redshift',
                    'dbname': 'redshift',
                    'user': 'root',
                    'host': 'thishostshouldnotexist',
                    'pass': 'password',
                    'port': 5439,
                    'schema': 'public'
                }
            },
            'target': 'test'
        }

        project_cfg = {
            'name': 'X',
            'version': '0.1',
            'profile': 'test',
            'project-root': '/tmp/dbt/does-not-exist',
            'quoting': {
                'identifier': False,
                'schema': True,
            }
        }

        self.config = config_from_parts_or_dicts(project_cfg, profile_cfg)
        self._adapter = None

    @property
    def adapter(self):
        if self._adapter is None:
            self._adapter = RedshiftAdapter(self.config)
        return self._adapter

    def test_implicit_database_conn(self):
        creds = RedshiftAdapter.ConnectionManager.get_credentials(self.config.credentials)
        self.assertEqual(creds, self.config.credentials)

    def test_explicit_database_conn(self):
        self.config.method = 'database'

        creds = RedshiftAdapter.ConnectionManager.get_credentials(self.config.credentials)
        self.assertEqual(creds, self.config.credentials)

    def test_explicit_iam_conn(self):
        self.config.credentials = self.config.credentials.replace(
            method='iam',
            cluster_id='my_redshift',
            iam_duration_seconds=1200
        )

        with mock.patch.object(RedshiftAdapter.ConnectionManager, 'fetch_cluster_credentials', new=fetch_cluster_credentials):
            creds = RedshiftAdapter.ConnectionManager.get_credentials(self.config.credentials)

        expected_creds = self.config.credentials.replace(password='tmp_password')
        self.assertEqual(creds, expected_creds)

    def test_invalid_auth_method(self):
        # we have to set method this way, otherwise it won't validate
        self.config.credentials.method = 'badmethod'

        with self.assertRaises(FailedToConnectException) as context:
            with mock.patch.object(RedshiftAdapter.ConnectionManager, 'fetch_cluster_credentials', new=fetch_cluster_credentials):
                RedshiftAdapter.ConnectionManager.get_credentials(self.config.credentials)

        self.assertTrue('badmethod' in context.exception.msg)

    def test_invalid_iam_no_cluster_id(self):
        self.config.credentials = self.config.credentials.replace(method='iam')
        with self.assertRaises(FailedToConnectException) as context:
            with mock.patch.object(RedshiftAdapter.ConnectionManager, 'fetch_cluster_credentials', new=fetch_cluster_credentials):
                RedshiftAdapter.ConnectionManager.get_credentials(self.config.credentials)

        self.assertTrue("'cluster_id' must be provided" in context.exception.msg)

    def test_cancel_open_connections_empty(self):
        self.assertEqual(len(list(self.adapter.cancel_open_connections())), 0)

    def test_cancel_open_connections_master(self):
        key = self.adapter.connections.get_thread_identifier()
        self.adapter.connections.thread_connections[key] = mock_connection('master')
        self.assertEqual(len(list(self.adapter.cancel_open_connections())), 0)

    def test_cancel_open_connections_single(self):
        master = mock_connection('master')
        model = mock_connection('model')
        model.handle.get_backend_pid.return_value = 42

        key = self.adapter.connections.get_thread_identifier()
        self.adapter.connections.thread_connections.update({
            key: master,
            1: model,
        })
        with mock.patch.object(self.adapter.connections, 'add_query') as add_query:
            query_result = mock.MagicMock()
            add_query.return_value = (None, query_result)

            self.assertEqual(len(list(self.adapter.cancel_open_connections())), 1)

            add_query.assert_called_once_with('select pg_terminate_backend(42)')

        master.handle.get_backend_pid.assert_not_called()

    @mock.patch('dbt.adapters.postgres.connections.psycopg2')
    def test_default_keepalive(self, psycopg2):
        connection = self.adapter.acquire_connection('dummy')

        psycopg2.connect.assert_called_once_with(
            dbname='redshift',
            user='root',
            host='thishostshouldnotexist',
            password='password',
            port=5439,
            connect_timeout=10,
            keepalives_idle=240
        )

    @mock.patch('dbt.adapters.postgres.connections.psycopg2')
    def test_changed_keepalive(self, psycopg2):
        self.config.credentials = self.config.credentials.replace(keepalives_idle=256)
        connection = self.adapter.acquire_connection('dummy')

        psycopg2.connect.assert_called_once_with(
            dbname='redshift',
            user='root',
            host='thishostshouldnotexist',
            password='password',
            port=5439,
            connect_timeout=10,
            keepalives_idle=256)

    @mock.patch('dbt.adapters.postgres.connections.psycopg2')
    def test_search_path(self, psycopg2):
        self.config.credentials = self.config.credentials.replace(search_path="test")
        connection = self.adapter.acquire_connection('dummy')

        psycopg2.connect.assert_called_once_with(
            dbname='redshift',
            user='root',
            host='thishostshouldnotexist',
            password='password',
            port=5439,
            connect_timeout=10,
            options="-c search_path=test",
            keepalives_idle=240)

    @mock.patch('dbt.adapters.postgres.connections.psycopg2')
    def test_search_path_with_space(self, psycopg2):
        self.config.credentials = self.config.credentials.replace(search_path="test test")
        connection = self.adapter.acquire_connection('dummy')

        psycopg2.connect.assert_called_once_with(
            dbname='redshift',
            user='root',
            host='thishostshouldnotexist',
            password='password',
            port=5439,
            connect_timeout=10,
            options="-c search_path=test\ test",
            keepalives_idle=240)

    @mock.patch('dbt.adapters.postgres.connections.psycopg2')
    def test_set_zero_keepalive(self, psycopg2):
        self.config.credentials = self.config.credentials.replace(keepalives_idle=0)
        connection = self.adapter.acquire_connection('dummy')

        psycopg2.connect.assert_called_once_with(
            dbname='redshift',
            user='root',
            host='thishostshouldnotexist',
            password='password',
            port=5439,
            connect_timeout=10)
