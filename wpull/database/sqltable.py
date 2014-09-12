'''SQLAlchemy table implementations.'''
import abc
import contextlib
import logging

from sqlalchemy.engine import create_engine
from sqlalchemy.orm.session import sessionmaker
from sqlalchemy.pool import SingletonThreadPool
from sqlalchemy.sql.expression import insert, update, select, and_, delete
from sqlalchemy.sql.functions import func
import sqlalchemy.event

from wpull.database.base import BaseURLTable, NotFound
from wpull.database.sqlmodel import URL, URLString, Visit, DBBase
from wpull.item import Status


_logger = logging.getLogger(__name__)


class BaseSQLURLTable(BaseURLTable):
    @abc.abstractproperty
    def _session_maker(self):
        pass

    @contextlib.contextmanager
    def _session(self):
        """Provide a transactional scope around a series of operations."""
        # Taken from the session docs.
        session = self._session_maker()
        try:
            yield session
            session.commit()
        except:
            session.rollback()
            raise
        finally:
            session.close()

    def count(self):
        with self._session() as session:
            return session.query(URL).count()

    def get_one(self, url):
        with self._session() as session:
            result = session.query(URL).filter_by(url=url).first()

            if not result:
                raise NotFound()
            else:
                return result.to_plain()

    def get_all(self):
        with self._session() as session:
            for item in session.query(URL):
                yield item.to_plain()

    def add_many(self, new_urls, **kwargs):
        assert not isinstance(new_urls, (str, bytes)), \
            'Expected a list-like. Got {}'.format(new_urls)
        referrer = kwargs.pop('referrer', None)
        top_url = kwargs.pop('top_url', None)
        url_strings = list(new_urls)
        added_urls = list()

        if referrer:
            url_strings.append(referrer)

        if top_url:
            url_strings.append(top_url)

        with self._session() as session:
            query = insert(URLString, [{'url': url} for url in url_strings])\
                .prefix_with('OR IGNORE')
            session.execute(query)

            last_primary_key = session.query(func.max(URL.id)).scalar()

            for url in new_urls:
                values = {}
                values = dict(status=Status.todo)
                values.update(**kwargs)

                values['url_str_id'] = select([URLString.id])\
                    .where(URLString.url == url)

                if referrer:
                    values['referrer_id'] = select([URLString.id])\
                        .where(URLString.url == referrer)
                if top_url:
                    values['top_url_str_id'] = select([URLString.id])\
                        .where(URLString.url == top_url)

                query = insert(URL, [values]).prefix_with('OR IGNORE')
                result = session.execute(query)

                if result.inserted_primary_key[0] != last_primary_key:
                    last_primary_key = result.inserted_primary_key[0]
                    added_urls.append(url)

        return added_urls

    def check_out(self, filter_status, level=None):
        with self._session() as session:
            if level is None:
                url_record = session.query(URL).filter_by(
                    status=filter_status).first()
            else:
                url_record = session.query(URL)\
                    .filter(
                        URL.status == filter_status,
                        URL.level < level,
                ).first()

            if not url_record:
                raise NotFound()

            url_record.status = Status.in_progress

            return url_record.to_plain()

    def check_in(self, url, new_status, increment_try_count=True, **kwargs):
        with self._session() as session:
            values = {
                URL.status: new_status
            }

            for key, value in kwargs.items():
                values[getattr(URL, key)] = value

            if increment_try_count:
                values[URL.try_count] = URL.try_count + 1

            query = update(URL).values(values).where(URL.url == url)

            session.execute(query)

    def update_one(self, url, **kwargs):
        with self._session() as session:
            values = {}

            for key, value in kwargs.items():
                values[getattr(URL, key)] = value

            query = update(URL).values(values).where(URL.url == url)

            session.execute(query)

    def release(self):
        with self._session() as session:
            session.query(URL)\
                .filter_by(status=Status.in_progress)\
                .update({URL.status: Status.todo})

    def remove_many(self, urls):
        assert not isinstance(urls, (str, bytes)), \
            'Expected list-like. Got {}.'.format(urls)

        with self._session() as session:
            for url in urls:
                url_str_id = session.query(URLString.id)\
                    .filter_by(url=url).scalar()
                query = delete(URL).where(URL.url_str_id == url_str_id)
                session.execute(query)

    def add_visits(self, visits):
        with self._session() as session:
            for url, warc_id, payload_digest in visits:
                session.execute(
                    insert(Visit).prefix_with('OR IGNORE'),
                    dict(
                        url=url,
                        warc_id=warc_id,
                        payload_digest=payload_digest
                    )
                )

    def get_revisit_id(self, url, payload_digest):
        query = select([Visit.warc_id]).where(
            and_(
                Visit.url == url,
                Visit.payload_digest == payload_digest
            )
        )

        with self._session() as session:
            row = session.execute(query).first()

            if row:
                return row.warc_id


class SQLiteURLTable(BaseSQLURLTable):
    '''URL table with SQLite storage.

    Args:
        path: A SQLite filename
    '''
    def __init__(self, path=':memory:'):
        super().__init__()
        # We use a SingletonThreadPool always because we are using WAL
        # and want SQLite to handle the checkpoints. Otherwise NullPool
        # will open and close the connection rapidly, defeating the purpose
        # of WAL.
        self._engine = create_engine(
            'sqlite:///{0}'.format(path), poolclass=SingletonThreadPool)
        sqlalchemy.event.listen(
            self._engine, 'connect', self._apply_pragmas_callback)
        DBBase.metadata.create_all(self._engine)
        self._session_maker_instance = sessionmaker(bind=self._engine)

    @classmethod
    def _apply_pragmas_callback(cls, connection, record):
        '''Set SQLite pragmas.

        Write-ahead logging, synchronous=NORMAL is used.
        '''
        _logger.debug('Setting pragmas.')
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA synchronous=NORMAL')

    @property
    def _session_maker(self):
        return self._session_maker_instance

    def close(self):
        self._engine.dispose()


class GenericSQLURLTable(BaseSQLURLTable):
    '''URL table using SQLAlchemy without any customizations.

    Args:
        url: A SQLAlchemy database URL.
    '''
    def __init__(self, url):
        super().__init__()
        self._engine = create_engine(url)
        DBBase.metadata.create_all(self._engine)
        self._session_maker_instance = sessionmaker(bind=self._engine)

    @property
    def _session_maker(self):
        return self._session_maker_instance

    def close(self):
        self._engine.dispose()


URLTable = SQLiteURLTable
'''The default URL table implementation.'''


__all__ = (
    'BaseSQLURLTable', 'SQLiteURLTable', 'GenericSQLURLTable', 'URLTable'
)
