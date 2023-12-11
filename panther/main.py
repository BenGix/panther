import contextlib
import logging
import sys
import types
from collections.abc import Callable
from logging.config import dictConfig
from pathlib import Path
from threading import Thread

import panther.logging
from panther import status
from panther._load_configs import *
from panther._utils import clean_traceback_message, http_response
from panther.background_tasks import background_tasks
from panther.cli.utils import print_info
from panther.configs import config
from panther.exceptions import APIException, PantherException
from panther.monitoring import Monitoring
from panther.request import Request
from panther.response import Response
from panther.routings import collect_path_variables, find_endpoint


dictConfig(panther.logging.LOGGING)
logger = logging.getLogger('panther')


class Panther:
    def __init__(self, name: str, configs=None, urls: dict | None = None):
        self._configs = configs
        self._urls = urls
        config['base_dir'] = Path(name).resolve().parent

        try:
            self.load_configs()
        except Exception as e:  # noqa: BLE001
            if isinstance(e, PantherException):
                logger.error(e.args[0])
            else:
                logger.error(clean_traceback_message(e))
            sys.exit()

        # Monitoring
        self.monitoring = Monitoring(is_active=config['monitoring'])

        # Print Info
        print_info(config)

        # Start Websocket Listener (Redis Required)
        if config['has_ws']:
            Thread(
                target=self.websocket_connections,
                daemon=True,
                args=(self.ws_redis_connection,),
            ).start()

    def load_configs(self) -> None:

        # Check & Read The Configs File
        self.configs = load_configs_file(self._configs)

        # Put Variables In "config" (Careful about the ordering)
        config['secret_key'] = load_secret_key(self.configs)
        config['monitoring'] = load_monitoring(self.configs)
        config['log_queries'] = load_log_queries(self.configs)
        config['background_tasks'] = load_background_tasks(self.configs)
        config['throttling'] = load_throttling(self.configs)
        config['default_cache_exp'] = load_default_cache_exp(self.configs)
        config['middlewares'] = load_middlewares(self.configs)
        config['reversed_middlewares'] = config['middlewares'][::-1]
        config['user_model'] = load_user_model(self.configs)
        config['authentication'] = load_authentication_class(self.configs)
        config['jwt_config'] = load_jwt_config(self.configs)
        config['models'] = collect_all_models()

        # Initialize Background Tasks
        if config['background_tasks']:
            background_tasks.initialize()

        # Load URLs should be one of the last calls in load_configs,
        #   because it will read all files and loads them.
        config['flat_urls'], config['urls'] = load_urls(self.configs, urls=self._urls)
        config['urls']['_panel'] = load_panel_urls()

        self._create_ws_connections_instance()

    def _create_ws_connections_instance(self):
        from panther.base_websocket import Websocket
        from panther.websocket import WebsocketConnections

        # Check do we have ws endpoint
        for endpoint in config['flat_urls'].values():
            if not isinstance(endpoint, types.FunctionType) and issubclass(endpoint, Websocket):
                config['has_ws'] = True
                break
        else:
            config['has_ws'] = False

        # Create websocket connections instance
        if config['has_ws']:
            config['websocket_connections'] = self.websocket_connections = WebsocketConnections()
            # Websocket Redis Connection
            for middleware in config['middlewares']:
                if middleware.__class__.__name__ == 'RedisMiddleware':
                    self.ws_redis_connection = middleware.redis_connection_for_ws()
                    break
            else:
                self.ws_redis_connection = None

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        """
        1.
            await func(scope, receive, send)
        2.
            async with asyncio.TaskGroup() as tg:
                tg.create_task(func(scope, receive, send))
        3.
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(func, scope, receive, send)
                await anyio.to_thread.run_sync(func, scope, receive, send)
        4.
            with ProcessPoolExecutor() as e:
                e.submit(func, scope, receive, send)
        """
        func = self.handle_http if scope['type'] == 'http' else self.handle_ws
        await func(scope=scope, receive=receive, send=send)

    async def handle_ws(self, scope: dict, receive: Callable, send: Callable) -> None:
        from panther.websocket import GenericWebsocket, Websocket
        monitoring = Monitoring(is_active=config['monitoring'], is_ws=True)

        temp_connection = Websocket(scope=scope, receive=receive, send=send)
        await monitoring.before(request=temp_connection)

        endpoint, found_path = find_endpoint(path=temp_connection.path)
        if endpoint is None:
            await monitoring.after('Rejected')
            return await temp_connection.close(status.WS_1000_NORMAL_CLOSURE)
        path_variables: dict = collect_path_variables(request_path=temp_connection.path, found_path=found_path)

        if not issubclass(endpoint, GenericWebsocket):
            logger.critical(f'You may have forgotten to inherit from GenericWebsocket on the {endpoint.__name__}()')
            await monitoring.after('Rejected')
            return await temp_connection.close(status.WS_1014_BAD_GATEWAY)

        del temp_connection
        connection = endpoint(scope=scope, receive=receive, send=send)
        connection.set_path_variables(path_variables=path_variables)

        # Call 'Before' Middlewares
        for middleware in config['middlewares']:
            try:
                connection = await middleware.before(request=connection)
            except APIException:
                await connection.close()
                break
        else:
            await self.websocket_connections.new_connection(connection=connection)
            await monitoring.after('Accepted')
            await connection.listen()

        # Call 'After' Middleware
        for middleware in config['reversed_middlewares']:
            with contextlib.suppress(APIException):
                await middleware.after(response=connection)

        await monitoring.after('Closed')
        return None

    async def handle_http(self, scope: dict, receive: Callable, send: Callable) -> None:
        request = Request(scope=scope, receive=receive, send=send)

        # Monitoring
        await self.monitoring.before(request=request)

        # Read Request Payload
        await request.read_body()

        # Find Endpoint
        endpoint, found_path = find_endpoint(path=request.path)
        path_variables: dict = collect_path_variables(request_path=request.path, found_path=found_path)

        if endpoint is None:
            return await http_response(
                send,
                status_code=status.HTTP_404_NOT_FOUND,
                monitoring=self.monitoring,
                exception=True,
            )

        try:  # They Both(middleware.before() & _endpoint()) Have The Same Exception (APIException)
            # Call 'Before' Middlewares
            for middleware in config['middlewares']:
                request = await middleware.before(request=request)

            # Function
            if isinstance(endpoint, types.FunctionType):
                # Function Doesn't Have @API Decorator
                if not hasattr(endpoint, '__wrapped__'):
                    logger.critical(f'You may have forgotten to use @API on the {endpoint.__name__}()')
                    return await http_response(
                        send,
                        status_code=status.HTTP_501_NOT_IMPLEMENTED,
                        monitoring=self.monitoring,
                        exception=True,
                    )

                # Declare Endpoint
                _endpoint = endpoint

            # Class
            else:
                from panther.app import GenericAPI

                if not issubclass(endpoint, GenericAPI):
                    logger.critical(f'You may have forgotten to inherit from GenericAPI on the {endpoint.__name__}()')
                    return await http_response(
                        send,
                        status_code=status.HTTP_501_NOT_IMPLEMENTED,
                        monitoring=self.monitoring,
                        exception=True,
                    )
                # Declare Endpoint
                _endpoint = endpoint.call_method

            # Call Endpoint
            response = await _endpoint(request=request, **path_variables)

        except APIException as e:
            response = self.handle_exceptions(e)
        except Exception as e:  # noqa: BLE001
            # Every unhandled exception in Panther or code will catch here
            exception = clean_traceback_message(exception=e)
            logger.critical(exception)

            return await http_response(
                send,
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                monitoring=self.monitoring,
                exception=True,
            )

        # Call 'After' Middleware
        for middleware in config['reversed_middlewares']:
            try:
                response = await middleware.after(response=response)
            except APIException as e:  # noqa: PERF203
                response = self.handle_exceptions(e)

        await http_response(
            send,
            status_code=response.status_code,
            monitoring=self.monitoring,
            headers=response.headers,
            body=response.body,
        )

    @classmethod
    def handle_exceptions(cls, e: APIException, /) -> Response:
        return Response(
            data=e.detail if isinstance(e.detail, dict) else {'detail': e.detail},
            status_code=e.status_code,
        )
