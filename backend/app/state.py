import os

import redis

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
r = redis.Redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=5, socket_timeout=5)


def release_owned_lock(client, key, owner):
    return client.eval(
        "if redis.call('get', KEYS[1]) == ARGV[1] then "
        "return redis.call('del', KEYS[1]) else return 0 end",
        1, key, owner,
    )


def refresh_owned_lock(client, key, owner, ttl):
    return client.eval(
        "if redis.call('get', KEYS[1]) == ARGV[1] then "
        "return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end",
        1, key, owner, ttl,
    )


def is_aborted(task_id):
    return r.get(f"abort:{task_id}") == "1"


def mark_aborted(task_id):
    r.set(f"abort:{task_id}", "1")
