import redis

r = redis.Redis(host="redis", port=6379, decode_responses=True)

def is_aborted(task_id):
    return r.get(f"abort:{task_id}") == "1"

def mark_aborted(task_id):
    r.set(f"abort:{task_id}", "1")
