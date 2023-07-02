> <b>Variable:</b> `LOG_QUERIES` 
> 
> <b>Type:</b> `bool` 
> 
> <b>Default:</b> `False`

Panther has a `log_query` decorator on queries that process the `perf_time` of every query

Make sure it is `False` on production 

#### Log Example:

```python
INFO:     | 2023-03-19 20:37:27 | Query -->  User.insert_one() --> 1.6 ms
```

#### The Log Query Decorator

```python
def log_query(func):
    def log(*args, **kwargs):
        if config['log_queries'] is False:
            return func(*args, **kwargs)
        start = perf_counter()
        response = func(*args, **kwargs)
        end = perf_counter()
        class_name = args[0].__name__ if hasattr(args[0], '__name__') else args[0].__class__.__name__
        query_logger.info(f'\033[1mQuery -->\033[0m  {class_name}.{func.__name__}() --> {(end - start) * 1_000:.2} ms')
        return response
    return log
```