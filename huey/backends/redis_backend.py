import re
import time

import redis
from redis.exceptions import ConnectionError

from huey.backends.base import BaseDataStore
from huey.backends.base import BaseEventEmitter
from huey.backends.base import BaseQueue
from huey.backends.base import BaseSchedule
from huey.utils import EmptyData


def clean_name(name):
    return re.sub('[^a-z0-9]', '', name)


def get_connection(**config):
    try:
        url = config.pop('url')
    except KeyError:
        return redis.Redis(**config)
    else:
        return redis.Redis.from_url(url, **config)


class RedisQueue(BaseQueue):
    """
    A simple Queue that uses the redis to store messages
    """

    def __init__(self, name, **connection):
        """
        connection = {
            'host': 'localhost',
            'port': 6379,
            'db': 0,
        }
        """
        super(RedisQueue, self).__init__(name, **connection)

        self.queue_name = 'huey.redis.%s' % clean_name(name)
        self.conn = get_connection(**connection)

    def write(self, data):
        self.conn.lpush(self.queue_name, data)

    def read(self):
        return self.conn.rpop(self.queue_name)

    def remove(self, data):
        return self.conn.lrem(self.queue_name, data)

    def flush(self):
        self.conn.delete(self.queue_name)

    def __len__(self):
        return self.conn.llen(self.queue_name)


class RedisBlockingQueue(RedisQueue):
    """
    Use the blocking right pop, should result in messages getting
    executed close to immediately by the consumer as opposed to
    being polled for
    """
    blocking = True

    def __init__(self, name, read_timeout=None, **connection):
        """
        connection = {
            'host': 'localhost',
            'port': 6379,
            'db': 0,
        }
        """
        super(RedisBlockingQueue, self).__init__(name, **connection)
        self.read_timeout = read_timeout

    def read(self):
        try:
            return self.conn.brpop(
                self.queue_name,
                timeout=self.read_timeout)[1]
        except (ConnectionError, TypeError, IndexError):
            # unfortunately, there is no way to differentiate a socket timing
            # out and a host being unreachable
            return None


# a custom lua script to pass to redis that will read tasks from the schedule
# and atomically pop them from the sorted set and return them.
# it won't return anything if it isn't able to remove the items it reads.
SCHEDULE_POP_LUA = """
local key = KEYS[1]
local unix_ts = ARGV[1]
local res = redis.call('zrangebyscore', key, '-inf', unix_ts)
if #res and redis.call('zremrangebyscore', key, '-inf', unix_ts) == #res then
    return res
end
"""


class RedisSchedule(BaseSchedule):
    def __init__(self, name, **connection):
        super(RedisSchedule, self).__init__(name, **connection)

        self.key = 'huey.schedule.%s' % clean_name(name)
        self.conn = get_connection(**connection)
        self._pop = self.conn.register_script(SCHEDULE_POP_LUA)

    def convert_ts(self, ts):
        return time.mktime(ts.timetuple())

    def add(self, data, ts):
        self.conn.zadd(self.key, data, self.convert_ts(ts))

    def read(self, ts):
        unix_ts = self.convert_ts(ts)
        # invoke the redis lua script that will atomically pop off
        # all the tasks older than the given timestamp
        tasks = self._pop(keys=[self.key], args=[unix_ts])
        return [] if tasks is None else tasks

    def flush(self):
        self.conn.delete(self.key)


class RedisDataStore(BaseDataStore):
    def __init__(self, name, **connection):
        super(RedisDataStore, self).__init__(name, **connection)

        self.storage_name = 'huey.results.%s' % clean_name(name)
        self.conn = get_connection(**connection)

    def put(self, key, value):
        self.conn.hset(self.storage_name, key, value)

    def peek(self, key):
        if self.conn.hexists(self.storage_name, key):
            return self.conn.hget(self.storage_name, key)
        return EmptyData

    def get(self, key):
        val = self.peek(key)
        if val is not EmptyData:
            self.conn.hdel(self.storage_name, key)
        return val

    def flush(self):
        self.conn.delete(self.storage_name)


class RedisEventEmitter(BaseEventEmitter):
    def __init__(self, channel, **connection):
        super(RedisEventEmitter, self).__init__(channel, **connection)
        self.conn = get_connection(**connection)

    def emit(self, message):
        self.conn.publish(self.channel, message)


Components = (RedisBlockingQueue, RedisDataStore, RedisSchedule,
              RedisEventEmitter)
