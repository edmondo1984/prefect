import asyncio
import logging
import queue
import sys
import threading
import time
import uuid
import warnings
from contextlib import nullcontext
from functools import partial
from unittest.mock import ANY, MagicMock

import pendulum
import pytest

import prefect
import prefect.logging.configuration
import prefect.settings
from prefect import flow, task
from prefect.context import FlowRunContext, TaskRunContext
from prefect.infrastructure import Process
from prefect.infrastructure.submission import submit_flow_run
from prefect.logging.configuration import (
    DEFAULT_LOGGING_SETTINGS_PATH,
    load_logging_config,
    setup_logging,
)
from prefect.logging.handlers import OrionHandler, OrionLogWorker
from prefect.logging.loggers import (
    flow_run_logger,
    get_logger,
    get_run_logger,
    task_run_logger,
)
from prefect.orion.schemas.actions import LogCreate
from prefect.results import _retrieve_result
from prefect.settings import (
    PREFECT_LOGGING_LEVEL,
    PREFECT_LOGGING_ORION_BATCH_INTERVAL,
    PREFECT_LOGGING_ORION_BATCH_SIZE,
    PREFECT_LOGGING_ORION_ENABLED,
    PREFECT_LOGGING_ORION_MAX_LOG_SIZE,
    PREFECT_LOGGING_SETTINGS_PATH,
    temporary_settings,
)
from prefect.testing.utilities import AsyncMock


@pytest.fixture
def dictConfigMock(monkeypatch):
    mock = MagicMock()
    monkeypatch.setattr("logging.config.dictConfig", mock)
    # Reset the process global since we're testing `setup_logging`
    old = prefect.logging.configuration.PROCESS_LOGGING_CONFIG
    prefect.logging.configuration.PROCESS_LOGGING_CONFIG = None
    yield mock
    prefect.logging.configuration.PROCESS_LOGGING_CONFIG = old


@pytest.fixture
async def logger_test_deployment(orion_client):
    """
    A deployment with a flow that returns information about the given loggers
    """

    @prefect.flow
    def my_flow(loggers=["foo", "bar", "prefect"]):
        import logging

        settings = {}

        for logger_name in loggers:
            logger = logging.getLogger(logger_name)
            settings[logger_name] = {
                "handlers": [handler.name for handler in logger.handlers],
                "level": logger.level,
            }
            logger.info(f"Hello from {logger_name}")

        return settings

    flow_id = await orion_client.create_flow(my_flow)

    deployment_id = await orion_client.create_deployment(
        flow_id=flow_id,
        name="logger_test_deployment",
        manifest_path="file.json",
    )

    return deployment_id


def test_setup_logging_uses_default_path(tmp_path, dictConfigMock):
    with temporary_settings(
        {PREFECT_LOGGING_SETTINGS_PATH: tmp_path.joinpath("does-not-exist.yaml")}
    ):
        expected_config = load_logging_config(DEFAULT_LOGGING_SETTINGS_PATH)
        setup_logging()

    dictConfigMock.assert_called_once_with(expected_config)


def test_setup_logging_allows_repeated_calls(dictConfigMock):
    setup_logging()
    dictConfigMock.assert_called_once()
    setup_logging()
    dictConfigMock.assert_called_once()


def test_setup_logging_warns_on_repeated_calls_with_new_settings(dictConfigMock):
    setup_logging()
    dictConfigMock.assert_called_once()
    with pytest.warns(
        UserWarning, match="only be setup once per process.* will be ignored"
    ):
        with temporary_settings({PREFECT_LOGGING_LEVEL: "ERROR"}):
            setup_logging()
    dictConfigMock.assert_called_once()


def test_setup_logging_uses_settings_path_if_exists(tmp_path, dictConfigMock):
    config_file = tmp_path.joinpath("exists.yaml")
    config_file.write_text("foo: bar")

    with temporary_settings({PREFECT_LOGGING_SETTINGS_PATH: config_file}):

        setup_logging()
        expected_config = load_logging_config(tmp_path.joinpath("exists.yaml"))

    dictConfigMock.assert_called_once_with(expected_config)


def test_setup_logging_uses_env_var_overrides(tmp_path, dictConfigMock, monkeypatch):

    with temporary_settings(
        {PREFECT_LOGGING_SETTINGS_PATH: tmp_path.joinpath("does-not-exist.yaml")}
    ):
        expected_config = load_logging_config(DEFAULT_LOGGING_SETTINGS_PATH)
    env = {}

    # Test setting a value for a simple key
    env["PREFECT_LOGGING_HANDLERS_ORION_LEVEL"] = "ORION_LEVEL_VAL"
    expected_config["handlers"]["orion"]["level"] = "ORION_LEVEL_VAL"

    # Test setting a value for the root logger
    env["PREFECT_LOGGING_ROOT_LEVEL"] = "ROOT_LEVEL_VAL"
    expected_config["root"]["level"] = "ROOT_LEVEL_VAL"

    # Test setting a value where the a key contains underscores
    env["PREFECT_LOGGING_FORMATTERS_FLOW_RUNS_DATEFMT"] = "UNDERSCORE_KEY_VAL"
    expected_config["formatters"]["flow_runs"]["datefmt"] = "UNDERSCORE_KEY_VAL"

    # Test setting a value where the key contains a period
    env["PREFECT_LOGGING_LOGGERS_PREFECT_FLOW_RUNS_LEVEL"] = "FLOW_RUN_VAL"

    expected_config["loggers"]["prefect.flow_runs"]["level"] = "FLOW_RUN_VAL"

    # Test setting a value that does not exist in the yaml config and should not be
    # set in the expected_config since there is no value to override
    env["PREFECT_LOGGING_FOO"] = "IGNORED"

    for var, value in env.items():
        monkeypatch.setenv(var, value)

    with temporary_settings(
        {PREFECT_LOGGING_SETTINGS_PATH: tmp_path.joinpath("does-not-exist.yaml")}
    ):
        setup_logging()

    dictConfigMock.assert_called_once_with(expected_config)


@pytest.mark.skip(reason="Will address with other infra compatibility improvements.")
@pytest.mark.enable_orion_handler
async def test_flow_run_respects_extra_loggers(orion_client, logger_test_deployment):
    """
    Runs a flow in a subprocess to check that PREFECT_LOGGING_EXTRA_LOGGERS works as
    intended. This avoids side-effects of modifying the loggers in this test run without
    confusing mocking.
    """
    flow_run = await orion_client.create_flow_run_from_deployment(
        logger_test_deployment
    )

    assert await submit_flow_run(
        flow_run, Process(env={"PREFECT_LOGGING_EXTRA_LOGGERS": "foo"})
    )

    state = (await orion_client.read_flow_run(flow_run.id)).state
    settings = await _retrieve_result(state)
    api_logs = await orion_client.read_logs()
    api_log_messages = [log.message for log in api_logs]

    extra_logger = logging.getLogger("prefect.extra")

    # Configures 'foo' to match 'prefect.extra'
    assert settings["foo"]["handlers"] == [
        handler.name for handler in extra_logger.handlers
    ]
    assert settings["foo"]["level"] == extra_logger.level
    assert "Hello from foo" in api_log_messages

    # Does not configure 'bar'
    assert settings["bar"]["handlers"] == []
    assert settings["bar"]["level"] == logging.NOTSET
    assert "Hello from bar" not in api_log_messages


@pytest.mark.parametrize("name", ["default", None, ""])
def test_get_logger_returns_prefect_logger_by_default(name):
    if name == "default":
        logger = get_logger()
    else:
        logger = get_logger(name)

    assert logger.name == "prefect"


def test_get_logger_returns_prefect_child_logger():
    logger = get_logger("foo")
    assert logger.name == "prefect.foo"


def test_get_logger_does_not_duplicate_prefect_prefix():
    logger = get_logger("prefect.foo")
    assert logger.name == "prefect.foo"


def test_default_level_is_applied_to_interpolated_yaml_values(dictConfigMock):
    with temporary_settings({PREFECT_LOGGING_LEVEL: "WARNING"}):
        expected_config = load_logging_config(DEFAULT_LOGGING_SETTINGS_PATH)

        assert expected_config["loggers"]["prefect"]["level"] == "WARNING"
        assert expected_config["loggers"]["prefect.extra"]["level"] == "WARNING"

        setup_logging()

    dictConfigMock.assert_called_once_with(expected_config)


@pytest.fixture
def mock_log_worker(monkeypatch):
    mock = MagicMock()
    monkeypatch.setattr("prefect.logging.handlers.OrionLogWorker", mock)
    return mock


@pytest.mark.enable_orion_handler
class TestOrionHandler:
    @pytest.fixture
    def handler(self):
        yield OrionHandler()

    @pytest.fixture
    def logger(self, handler):
        logger = logging.getLogger(__name__)
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        yield logger
        logger.removeHandler(handler)

    def test_handler_instances_share_log_worker(self):
        first = OrionHandler().get_worker(prefect.context.get_settings_context())
        second = OrionHandler().get_worker(prefect.context.get_settings_context())
        assert first is second
        assert len(OrionHandler.workers) == 1

    def test_log_workers_are_cached_by_profile(self):
        a = OrionHandler().get_worker(prefect.context.get_settings_context())
        b = OrionHandler().get_worker(
            prefect.context.get_settings_context().copy(update={"name": "foo"})
        )
        assert a is not b
        assert len(OrionHandler.workers) == 2

    def test_instantiates_log_worker(self, mock_log_worker):
        OrionHandler().get_worker(prefect.context.get_settings_context())
        mock_log_worker.assert_called_once_with(prefect.context.get_settings_context())
        mock_log_worker().start.assert_called_once_with()

    def test_worker_is_not_started_until_log_is_emitted(self, mock_log_worker, logger):
        mock_log_worker().start.assert_not_called()
        logger.setLevel(logging.INFO)
        logger.debug("test-task", extra={"flow_run_id": uuid.uuid4()})
        mock_log_worker().start.assert_not_called()
        logger.info("test-task", extra={"flow_run_id": uuid.uuid4()})
        mock_log_worker().start.assert_called()

    def test_worker_is_flushed_on_handler_close(self, mock_log_worker):
        handler = OrionHandler()
        handler.get_worker(prefect.context.get_settings_context())
        handler.close()
        mock_log_worker().flush.assert_called_once()
        # The worker cannot be stopped because it is a singleton and other handler
        # instances may be using it
        mock_log_worker().stop.assert_not_called()

    @pytest.mark.flaky
    async def test_logs_can_still_be_sent_after_close(
        self, logger, handler, flow_run, orion_client
    ):
        logger.info("Test", extra={"flow_run_id": flow_run.id})  # Start the logger
        handler.close()  # Close it
        logger.info("Test", extra={"flow_run_id": flow_run.id})
        handler.flush(block=True)

        logs = await orion_client.read_logs()
        assert len(logs) == 2

    async def test_logs_cannot_be_sent_after_worker_stop(
        self, logger, handler, flow_run, orion_client, capsys
    ):
        logger.info("Test", extra={"flow_run_id": flow_run.id})
        for worker in handler.workers.values():
            worker.stop()

        # Send a log that will not be sent
        logger.info("Test", extra={"flow_run_id": flow_run.id})

        logs = await orion_client.read_logs()
        assert len(logs) == 1

        output = capsys.readouterr()
        assert (
            "RuntimeError: Logs cannot be enqueued after the Orion log worker is stopped."
            in output.err
        )

    def test_worker_is_not_stopped_if_not_set_on_handler_close(self, mock_log_worker):
        OrionHandler().close()
        mock_log_worker().stop.assert_not_called()

    def test_sends_task_run_log_to_worker(self, logger, mock_log_worker, task_run):
        with TaskRunContext.construct(task_run=task_run):
            logger.info("test-task")

        expected = LogCreate.construct(
            flow_run_id=task_run.flow_run_id,
            task_run_id=task_run.id,
            name=logger.name,
            level=logging.INFO,
            message="test-task",
        ).dict(json_compatible=True)
        expected["timestamp"] = ANY  # Tested separately

        mock_log_worker().enqueue.assert_called_once_with(expected)

    def test_sends_flow_run_log_to_worker(self, logger, mock_log_worker, flow_run):
        with FlowRunContext.construct(flow_run=flow_run):
            logger.info("test-flow")

        expected = LogCreate.construct(
            flow_run_id=flow_run.id,
            task_run_id=None,
            name=logger.name,
            level=logging.INFO,
            message="test-flow",
        ).dict(json_compatible=True)
        expected["timestamp"] = ANY  # Tested separately

        mock_log_worker().enqueue.assert_called_once_with(expected)

    @pytest.mark.parametrize("with_context", [True, False])
    def test_respects_explicit_flow_run_id(
        self, logger, mock_log_worker, flow_run, with_context
    ):
        flow_run_id = uuid.uuid4()
        context = (
            FlowRunContext.construct(flow_run=flow_run)
            if with_context
            else nullcontext()
        )
        with context:
            logger.info("test-task", extra={"flow_run_id": flow_run_id})

        expected = LogCreate.construct(
            flow_run_id=flow_run_id,
            task_run_id=None,
            name=logger.name,
            level=logging.INFO,
            message="test-task",
        ).dict(json_compatible=True)
        expected["timestamp"] = ANY  # Tested separately

        mock_log_worker().enqueue.assert_called_once_with(expected)

    @pytest.mark.parametrize("with_context", [True, False])
    def test_respects_explicit_task_run_id(
        self, logger, mock_log_worker, flow_run, with_context, task_run
    ):
        task_run_id = uuid.uuid4()
        context = (
            TaskRunContext.construct(task_run=task_run)
            if with_context
            else nullcontext()
        )
        with FlowRunContext.construct(flow_run=flow_run):
            with context:
                logger.warning("test-task", extra={"task_run_id": task_run_id})

        expected = LogCreate.construct(
            flow_run_id=flow_run.id,
            task_run_id=task_run_id,
            name=logger.name,
            level=logging.WARNING,
            message="test-task",
        ).dict(json_compatible=True)
        expected["timestamp"] = ANY  # Tested separately

        mock_log_worker().enqueue.assert_called_once_with(expected)

    def test_does_not_emit_logs_below_level(self, logger, mock_log_worker):
        logger.setLevel(logging.WARNING)
        logger.info("test-task", extra={"flow_run_id": uuid.uuid4()})
        mock_log_worker().enqueue.assert_not_called()

    def test_explicit_task_run_id_still_requires_flow_run_id(
        self, logger, mock_log_worker
    ):
        task_run_id = uuid.uuid4()
        with pytest.warns(
            UserWarning, match="attempted to send logs .* without a flow run id"
        ):
            logger.info("test-task", extra={"task_run_id": task_run_id})

        mock_log_worker().enqueue.assert_not_called()

    def test_sets_timestamp_from_record_created_time(
        self, logger, mock_log_worker, flow_run, handler
    ):
        # Capture the record
        handler.emit = MagicMock(side_effect=handler.emit)

        with FlowRunContext.construct(flow_run=flow_run):
            logger.info("test-flow")

        record = handler.emit.call_args[0][0]
        log_json = mock_log_worker().enqueue.call_args[0][0]

        assert (
            log_json["timestamp"] == pendulum.from_timestamp(record.created).isoformat()
        )

    def test_sets_timestamp_from_time_if_missing_from_recrod(
        self, logger, mock_log_worker, flow_run, handler, monkeypatch
    ):
        def drop_created_and_emit(emit, record):
            record.created = None
            return emit(record)

        handler.emit = MagicMock(
            side_effect=partial(drop_created_and_emit, handler.emit)
        )

        now = time.time()
        monkeypatch.setattr("time.time", lambda: now)

        with FlowRunContext.construct(flow_run=flow_run):
            logger.info("test-flow")

        log_json = mock_log_worker().enqueue.call_args[0][0]

        assert log_json["timestamp"] == pendulum.from_timestamp(now).isoformat()

    def test_does_not_send_logs_that_opt_out(self, logger, mock_log_worker, task_run):
        with TaskRunContext.construct(task_run=task_run):
            logger.info("test", extra={"send_to_orion": False})

        mock_log_worker().enqueue.assert_not_called()

    def test_does_not_send_logs_when_handler_is_disabled(
        self, logger, mock_log_worker, task_run
    ):
        with temporary_settings(
            updates={PREFECT_LOGGING_ORION_ENABLED: "False"},
        ):
            with TaskRunContext.construct(task_run=task_run):
                logger.info("test")

        mock_log_worker().enqueue.assert_not_called()

    def test_does_not_send_logs_outside_of_run_context(
        self, logger, mock_log_worker, capsys
    ):
        # Warns in the main process
        with pytest.warns(
            UserWarning, match="attempted to send logs .* without a flow run id"
        ):
            logger.info("test")

        mock_log_worker().enqueue.assert_not_called()

        # No stderr output
        output = capsys.readouterr()
        assert output.err == ""

    def test_missing_context_warning_refers_to_caller_lineno(
        self, logger, mock_log_worker
    ):
        from inspect import currentframe, getframeinfo

        # Warns in the main process
        with pytest.warns(
            UserWarning, match="attempted to send logs .* without a flow run id"
        ) as warnings:
            logger.info("test")
            lineno = getframeinfo(currentframe()).lineno - 1
            # The above dynamic collects the line number so that added tests do not
            # break this tests

        mock_log_worker().enqueue.assert_not_called()
        assert warnings.pop().lineno == lineno

    def test_writes_logging_errors_to_stderr(
        self, logger, mock_log_worker, capsys, monkeypatch
    ):
        monkeypatch.setattr(
            "prefect.logging.handlers.OrionHandler.prepare",
            MagicMock(side_effect=RuntimeError("Oh no!")),
        )
        # No error raised
        logger.info("test")

        mock_log_worker().enqueue.assert_not_called()

        # Error is in stderr
        output = capsys.readouterr()
        assert "RuntimeError: Oh no!" in output.err

    def test_does_not_write_error_for_logs_outside_run_context_that_opt_out(
        self, logger, mock_log_worker, capsys
    ):
        logger.info("test", extra={"send_to_orion": False})

        mock_log_worker().enqueue.assert_not_called()
        output = capsys.readouterr()
        assert (
            "RuntimeError: Attempted to send logs to Orion without a flow run id."
            not in output.err
        )

    async def test_does_not_enqueue_logs_that_are_too_big(
        self, task_run, logger, capsys, mock_log_worker
    ):
        with TaskRunContext.construct(task_run=task_run):
            with temporary_settings(updates={PREFECT_LOGGING_ORION_MAX_LOG_SIZE: "1"}):
                logger.info("test")

        mock_log_worker().enqueue.assert_not_called()
        output = capsys.readouterr()
        assert "ValueError" in output.err
        assert "is greater than the max size of 1" in output.err


class TestOrionLogWorker:
    @pytest.fixture
    def log_json(self):
        return LogCreate(
            flow_run_id=uuid.uuid4(),
            task_run_id=uuid.uuid4(),
            name="test.logger",
            level=10,
            timestamp=pendulum.now("utc"),
            message="hello",
        ).dict(json_compatible=True)

    @pytest.fixture
    def worker(self, get_worker):
        yield get_worker()

    @pytest.fixture
    def get_worker(self):
        # Ensures that a worker is stopped _before_ the test is torn down. Otherwise,
        # remaining logs could be written by a background thread after all the tests
        # finish and the database has been reset.
        worker = None

        def get_worker():
            nonlocal worker
            worker = OrionLogWorker(prefect.context.get_settings_context())
            return worker

        yield get_worker

        if worker:
            worker.stop()
        else:
            warnings.warn("`get_worker` fixture was specified but not called.")

    def test_start_is_idempotent(self, worker):
        worker._send_thread = MagicMock()
        worker.start()
        worker.start()
        worker._send_thread.start.assert_called_once()

    def test_stop_is_idempotent(self, worker):
        worker._send_thread = MagicMock()
        worker._stop_event = MagicMock()
        worker._flush_event = MagicMock()
        worker.stop()
        worker._stop_event.set.assert_not_called()
        worker._flush_event.set.assert_not_called()
        worker._send_thread.join.assert_not_called()
        worker.start()
        worker.stop()
        worker.stop()
        worker._flush_event.set.assert_called_once()
        worker._stop_event.set.assert_called_once()
        worker._send_thread.join.assert_called_once()

    def test_enqueue(self, log_json, worker):
        worker.enqueue(log_json)
        assert worker._queue.get_nowait() == log_json

    async def test_send_logs_single_record(self, log_json, orion_client, worker):
        worker.enqueue(log_json)
        await worker.send_logs()
        logs = await orion_client.read_logs()
        assert len(logs) == 1
        assert logs[0].dict(include=log_json.keys(), json_compatible=True) == log_json

    async def test_send_logs_many_records(self, log_json, orion_client, worker):
        # Use the read limit as the count since we'd need multiple read calls otherwise
        count = prefect.settings.PREFECT_ORION_API_DEFAULT_LIMIT.value()
        log_json.pop("message")

        for i in range(count):
            new_log = log_json.copy()
            new_log["message"] = str(i)
            worker.enqueue(new_log)
        await worker.send_logs()

        logs = await orion_client.read_logs()
        assert len(logs) == count
        for log in logs:
            assert (
                log.dict(
                    include=log_json.keys(), exclude={"message"}, json_compatible=True
                )
                == log_json
            )
        assert len(set(log.message for log in logs)) == count, "Each log is unique"

    async def test_send_logs_retries_on_next_call_on_exception(
        self, log_json, orion_client, monkeypatch, capsys, worker
    ):
        create_logs = orion_client.create_logs
        monkeypatch.setattr(
            "prefect.client.OrionClient.create_logs",
            MagicMock(side_effect=ValueError("Test")),
        )

        worker.enqueue(log_json)
        await worker.send_logs()

        # Log moved from queue to pending logs
        assert worker._pending_logs == [log_json]
        with pytest.raises(queue.Empty):
            worker._queue.get_nowait()

        # Restore client
        monkeypatch.setattr(
            "prefect.client.OrionClient.create_logs",
            create_logs,
        )
        await worker.send_logs()

        logs = await orion_client.read_logs()
        assert len(logs) == 1

    @pytest.mark.parametrize("exiting", [True, False])
    async def test_send_logs_writes_exceptions_to_stderr(
        self, log_json, capsys, monkeypatch, exiting, worker
    ):
        monkeypatch.setattr(
            "prefect.client.OrionClient.create_logs",
            MagicMock(side_effect=ValueError("Test")),
        )

        worker.enqueue(log_json)
        await worker.send_logs(exiting=exiting)

        err = capsys.readouterr().err
        assert "--- Orion logging error ---" in err
        assert "ValueError: Test" in err
        if not exiting:
            assert "will attempt to send these logs again" in err
        else:
            assert "log worker is stopping and these logs will not be sent" in err

    async def test_send_logs_batches_by_size(self, log_json, monkeypatch, get_worker):
        test_log_size = sys.getsizeof(log_json)
        mock_create_logs = AsyncMock()
        monkeypatch.setattr("prefect.client.OrionClient.create_logs", mock_create_logs)

        with temporary_settings(
            updates={
                PREFECT_LOGGING_ORION_BATCH_SIZE: test_log_size + 1,
                PREFECT_LOGGING_ORION_MAX_LOG_SIZE: test_log_size,
            }
        ):
            worker = get_worker()
            worker.enqueue(log_json)
            worker.enqueue(log_json)
            worker.enqueue(log_json)
            await worker.send_logs()

        assert mock_create_logs.call_count == 3

    @pytest.mark.flaky(max_runs=3)
    async def test_logs_are_sent_when_started(
        self, log_json, orion_client, get_worker, monkeypatch
    ):
        event = threading.Event()
        unpatched_create_logs = orion_client.create_logs

        async def create_logs(self, *args, **kwargs):
            result = await unpatched_create_logs(*args, **kwargs)
            event.set()
            return result

        monkeypatch.setattr("prefect.client.OrionClient.create_logs", create_logs)

        with temporary_settings(
            updates={PREFECT_LOGGING_ORION_BATCH_INTERVAL: "0.001"}
        ):
            worker = get_worker()
            worker.enqueue(log_json)
            worker.start()
            worker.enqueue(log_json)

        # We want to ensure logs are written without the thread being joined
        await asyncio.sleep(0.01)
        event.wait()
        logs = await orion_client.read_logs()
        # TODO: CI failures sometimes find one log here instead of two.
        assert len(logs) == 2

    def test_batch_interval_is_respected(self, get_worker):

        with temporary_settings(updates={PREFECT_LOGGING_ORION_BATCH_INTERVAL: "5"}):
            worker = get_worker()
            worker._flush_event = MagicMock(return_val=False)
            worker.start()

            # Wait for the a loop to complete
            worker._send_logs_finished_event.wait(1)

        worker._flush_event.wait.assert_called_with(5)

    def test_flush_event_is_cleared(self, get_worker):
        with temporary_settings(updates={PREFECT_LOGGING_ORION_BATCH_INTERVAL: "5"}):
            worker = get_worker()
            worker._flush_event = MagicMock(return_val=False)
            worker.start()
            worker.flush(block=True)

        worker._flush_event.wait.assert_called_with(5)
        worker._flush_event.clear.assert_called()

    async def test_logs_are_sent_immediately_when_stopped(
        self, log_json, orion_client, get_worker
    ):
        # Set a long interval
        start_time = time.time()
        with temporary_settings(updates={PREFECT_LOGGING_ORION_BATCH_INTERVAL: "10"}):
            worker = get_worker()
            worker.enqueue(log_json)
            worker.start()
            worker.enqueue(log_json)
            worker.stop()
        end_time = time.time()

        assert (
            end_time - start_time
        ) < 5  # An arbitary time less than the 10s interval

        logs = await orion_client.read_logs()
        assert len(logs) == 2

    async def test_raises_on_enqueue_after_stop(self, worker, log_json):
        worker.start()
        worker.stop()
        with pytest.raises(
            RuntimeError, match="Logs cannot be enqueued after .* is stopped"
        ):
            worker.enqueue(log_json)

    async def test_raises_on_start_after_stop(self, worker, log_json):
        worker.start()
        worker.stop()
        with pytest.raises(RuntimeError, match="cannot be started after stopping"):
            worker.start()

    async def test_logs_are_sent_immediately_when_flushed(
        self, log_json, orion_client, get_worker
    ):
        # Set a long interval
        start_time = time.time()
        with temporary_settings(updates={PREFECT_LOGGING_ORION_BATCH_INTERVAL: "10"}):
            worker = get_worker()
            worker.enqueue(log_json)
            worker.start()
            worker.enqueue(log_json)
            worker.flush(block=True)
        end_time = time.time()

        assert (
            end_time - start_time
        ) < 5  # An arbitary time less than the 10s interval

        logs = await orion_client.read_logs()
        assert len(logs) == 2

    async def test_logs_can_be_flushed_repeatedly(
        self, log_json, orion_client, get_worker
    ):
        # Set a long interval
        start_time = time.time()
        with temporary_settings(updates={PREFECT_LOGGING_ORION_BATCH_INTERVAL: "10"}):
            worker = get_worker()
            worker.enqueue(log_json)
            worker.start()
            worker.enqueue(log_json)
            worker.flush()
            worker.flush()
            worker.enqueue(log_json)
            worker.flush(block=True)
        end_time = time.time()

        assert (
            end_time - start_time
        ) < 5  # An arbitary time less than the 10s interval

        logs = await orion_client.read_logs()
        assert len(logs) == 3


def test_flow_run_logger(flow_run):
    logger = flow_run_logger(flow_run)
    assert logger.name == "prefect.flow_runs"
    assert logger.extra == {
        "flow_run_name": flow_run.name,
        "flow_run_id": str(flow_run.id),
        "flow_name": "<unknown>",
    }


def test_flow_run_logger_with_flow(flow_run):
    @flow(name="foo")
    def test_flow():
        pass

    logger = flow_run_logger(flow_run, test_flow)
    assert logger.extra["flow_name"] == "foo"


def test_flow_run_logger_with_kwargs(flow_run):
    logger = flow_run_logger(flow_run, foo="test", flow_run_name="bar")
    assert logger.extra["foo"] == "test"
    assert logger.extra["flow_run_name"] == "bar"


def test_task_run_logger(task_run):
    logger = task_run_logger(task_run)
    assert logger.name == "prefect.task_runs"
    assert logger.extra == {
        "task_run_name": task_run.name,
        "task_run_id": str(task_run.id),
        "flow_run_id": str(task_run.flow_run_id),
        "flow_run_name": "<unknown>",
        "flow_name": "<unknown>",
        "task_name": "<unknown>",
    }


def test_task_run_logger_with_task(task_run):
    @task(name="task_run_logger_with_task")
    def test_task():
        pass

    logger = task_run_logger(task_run, test_task)
    assert logger.extra["task_name"] == "task_run_logger_with_task"


def test_task_run_logger_with_flow_run(task_run, flow_run):
    logger = task_run_logger(task_run, flow_run=flow_run)
    assert logger.extra["flow_run_id"] == str(task_run.flow_run_id)
    assert logger.extra["flow_run_name"] == flow_run.name


def test_task_run_logger_with_flow(task_run):
    @flow(name="foo")
    def test_flow():
        pass

    logger = task_run_logger(task_run, flow=test_flow)
    assert logger.extra["flow_name"] == "foo"


def test_task_run_logger_with_kwargs(task_run):
    logger = task_run_logger(task_run, foo="test", task_run_name="bar")
    assert logger.extra["foo"] == "test"
    assert logger.extra["task_run_name"] == "bar"


def test_run_logger_fails_outside_context():
    with pytest.raises(RuntimeError, match="no active flow or task run context"):
        get_run_logger()


async def test_run_logger_with_explicit_context(
    orion_client, flow_run, local_filesystem
):
    @task
    def foo():
        pass

    task_run = await orion_client.create_task_run(foo, flow_run.id, dynamic_key="")
    context = TaskRunContext(
        task=foo,
        task_run=task_run,
        client=orion_client,
        result_filesystem=local_filesystem,
    )

    logger = get_run_logger(context)

    assert logger.name == "prefect.task_runs"
    assert logger.extra == {
        "task_name": foo.name,
        "task_run_id": str(task_run.id),
        "task_run_name": task_run.name,
        "flow_run_id": str(flow_run.id),
        "flow_name": "<unknown>",
        "flow_run_name": "<unknown>",
    }


async def test_run_logger_with_explicit_context_overrides_existing(
    orion_client, flow_run, local_filesystem
):
    @task
    def foo():
        pass

    @task
    def bar():
        pass

    task_run = await orion_client.create_task_run(foo, flow_run.id, dynamic_key="")
    # Use `bar` instead of `foo` in context
    context = TaskRunContext(
        task=bar,
        task_run=task_run,
        client=orion_client,
        result_filesystem=local_filesystem,
    )

    logger = get_run_logger(context)
    assert logger.extra["task_name"] == bar.name


async def test_run_logger_in_flow(orion_client):
    @flow
    def test_flow():
        return get_run_logger()

    state = test_flow._run()
    flow_run = await orion_client.read_flow_run(state.state_details.flow_run_id)
    logger = state.result()
    assert logger.name == "prefect.flow_runs"
    assert logger.extra == {
        "flow_name": test_flow.name,
        "flow_run_id": str(flow_run.id),
        "flow_run_name": flow_run.name,
    }


async def test_run_logger_extra_data(orion_client):
    @flow
    def test_flow():
        return get_run_logger(foo="test", flow_name="bar")

    state = test_flow._run()
    flow_run = await orion_client.read_flow_run(state.state_details.flow_run_id)
    logger = state.result()
    assert logger.name == "prefect.flow_runs"
    assert logger.extra == {
        "flow_name": "bar",
        "foo": "test",
        "flow_run_id": str(flow_run.id),
        "flow_run_name": flow_run.name,
    }


async def test_run_logger_in_nested_flow(orion_client):
    @flow
    def child_flow():
        return get_run_logger()

    @flow
    def test_flow():
        return child_flow._run()

    child_state = test_flow._run().result()
    flow_run = await orion_client.read_flow_run(child_state.state_details.flow_run_id)
    logger = child_state.result()
    assert logger.name == "prefect.flow_runs"
    assert logger.extra == {
        "flow_name": child_flow.name,
        "flow_run_id": str(flow_run.id),
        "flow_run_name": flow_run.name,
    }


async def test_run_logger_in_task(orion_client):
    @task
    def test_task():
        return get_run_logger()

    @flow
    def test_flow():
        return test_task._run()

    flow_state = test_flow._run()
    flow_run = await orion_client.read_flow_run(flow_state.state_details.flow_run_id)
    task_state = flow_state.result()
    task_run = await orion_client.read_task_run(task_state.state_details.task_run_id)
    logger = task_state.result()
    assert logger.name == "prefect.task_runs"
    assert logger.extra == {
        "task_name": test_task.name,
        "task_run_id": str(task_run.id),
        "task_run_name": task_run.name,
        "flow_name": test_flow.name,
        "flow_run_id": str(flow_run.id),
        "flow_run_name": flow_run.name,
    }
