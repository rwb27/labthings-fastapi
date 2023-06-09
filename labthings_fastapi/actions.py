from __future__ import annotations
import datetime
import logging
import traceback
from collections import deque
from enum import Enum
from threading import Event, Thread, Lock, get_ident
from typing import Optional, Callable, Iterable, Any, TypeVar, Generic
import uuid
from typing import TYPE_CHECKING
import weakref
from pydantic.generics import GenericModel
from fastapi import FastAPI, HTTPException, Request

if TYPE_CHECKING:
    # We only need these imports for type hints, so this avoids circular imports.
    from .descriptors import ActionDescriptor
    from .thing import Thing

ACTION_INVOCATIONS_PATH = "/action_invocations"

class InvocationStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ERROR = "error"


InputT = TypeVar("InputT")
OutputT = TypeVar("OutputT")
class GenericInvocationModel(GenericModel, Generic[InputT, OutputT]):
    status: InvocationStatus
    id: uuid.UUID
    action: str
    href: str
    timeStarted: Optional[datetime.datetime]
    timeRequested: Optional[datetime.datetime]
    timeCompleted: Optional[datetime.datetime]
    input: InputT
    output: OutputT

InvocationModel = GenericInvocationModel[Any, Any]

class Invocation(Thread):
    """A Thread subclass that retains output values and tracks progress"""
    def __init__(
        self,
        action: ActionDescriptor,
        thing: Thing,
        input: Optional[dict[str, Any]] = None,
        default_stop_timeout: float = 5,
        log_len: int = 1000,
    ):
        Thread.__init__(self, daemon=True)

        # keep track of the corresponding ActionDescriptor
        self.action_ref = weakref.ref(action)
        self.thing_ref = weakref.ref(thing)
        self.input = input

        # A UUID for the Invocation (not the same as the threading.Thread ident)
        self._ID = uuid.uuid4()  # Task ID

        # Event to track if the user has requested stop
        self.stopping: Event = Event()
        self.default_stop_timeout: float = default_stop_timeout

        # Private state properties
        self._status_lock = Lock() # This Lock should be acquired before using properties below
        self._status: InvocationStatus = InvocationStatus.PENDING  # Task status
        self._return_value: Optional[Any] = None  # Return value
        self._request_time: datetime.datetime = datetime.datetime.now()
        self._start_time: Optional[datetime.datetime] = None  # Task start time
        self._end_time: Optional[datetime.datetime] = None  # Task end time
        self._exception: Optional[Exception] = None  # Propagate exceptions helpfully
        self._log = deque(maxlen=log_len)  # The log will hold dictionary objects with log information

    @property
    def id(self) -> uuid.UUID:
        """
        UUID for the thread. Note this not the same as the native thread ident.
        """
        return self._ID

    @property
    def output(self) -> Any:
        """
        Return value of the Action. If the Action is still running, returns None.
        """
        with self._status_lock:
            return self._return_value

    @property
    def log(self):
        """A list of log items generated by the Action."""
        with self._status_lock:
            return list(self._log)

    @property
    def status(self) -> InvocationStatus:
        """
        Current running status of the thread.

        ==============  =============================================
        Status          Meaning
        ==============  =============================================
        ``pending``     Not yet started
        ``running``     Currently in-progress
        ``completed``   Finished without error
        ``cancelled``   Thread stopped after a cancel request
        ``error``       Exception occured in thread
        ==============  =============================================
        """
        with self._status_lock:
            return self._status
    
    @property
    def action(self):
        return self.action_ref()
    
    @property
    def thing(self):
        return self.thing_ref()
    
    def response(self, request: Optional[Request] = None):
        if request:
            href = str(request.url_for("action_invocation", id=self.id))
        else:
            href = f"{ACTION_INVOCATIONS_PATH}/{self.id}"
        return InvocationModel(
            status=self.status,
            id=self.id,
            action=self.thing.path + self.action.name,
            href=href,
            timeStarted=self._start_time,
            timeCompleted=self._end_time,
            timeRequested=self._request_time,
            input=self.input,
            output=self.output
        )

    def run(self):
        """Overrides default threading.Thread run() method"""
        # Capture just this thread's log messages
        handler = ThreadLogHandler(self, self._log, self._status_lock)
        logging.getLogger().addHandler(handler)

        action = self.action
        thing = self.thing
        kwargs = self.input.dict() or {} # In the future, this may also support a single positional argument
        assert action is not None
        assert thing is not None

        with self._status_lock:
            self._status = InvocationStatus.RUNNING
            self._start_time = datetime.datetime.now()

        try:
            # The next line actually runs the action.
            logging.info(f"Running action with kwargs: {kwargs}")
            ret = action.__get__(thing)(**kwargs)

            with self._status_lock:
                self._return_value = ret
                self._status = InvocationStatus.COMPLETED
        except SystemExit as e:
            logging.error(e)
            with self._status_lock:
                self._status = InvocationStatus.CANCELLED
        except Exception as e:  # skipcq: PYL-W0703
            logging.error(traceback.format_exc())
            with self._status_lock:
                self._status = InvocationStatus.ERROR
                self._return_value = str(e)
                self._exception = e
            raise e
        finally:
            with self._status_lock:
                self._end_time = datetime.datetime.now()
            logging.getLogger().removeHandler(handler)  # Stop logging this thread
            # If we don't remove the log handler, it's a memory leak.


class ThreadLogHandler(logging.Handler):
    def __init__(
        self,
        thread: Invocation,
        dest: deque,
        lock: Lock,
        level=logging.INFO,
    ):
        """Set up a log handler that appends messages to a list.

        This log handler will first filter by ``thread``, if one is
        supplied.  This should be a ``threading.Thread`` object.
        Only log entries from the specified thread will be
        saved.

        ``dest`` should specify a list, to which we will append
        each log entry as it comes in.  If none is specified, a
        new list will be created.

        NB this log handler does not currently rotate or truncate
        the list - so if you use it on a thread that produces a
        lot of log messages, you may run into memory problems.


        """
        logging.Handler.__init__(self)
        self.setLevel(level)
        self.thread_ident = thread.ident
        self.dest = dest
        self.addFilter(self.check_thread)

    def check_thread(self, *_):
        """Determine if a thread matches the desired record

        :param record:

        """
        if get_ident() == self.thread_ident:
            return 1
        return 0

    def emit(self, record):
        """Save a log record to the destination deque"""
        with self.lock:
            self.dest.append(record)



class ActionManager:
    """A class to manage a collection of actions
    """
    def __init__(self):
        self._invocations = {}
        self._invocations_lock = Lock()

    @property
    def invocations(self):
        with self._invocations_lock:
            return list(self._invocations.values())
        
    def append_invocation(self, invocation: Invocation):
        with self._invocations_lock:
            self._invocations[invocation.id] = invocation
        
    def invoke_action(self, action: ActionDescriptor, thing: Thing, input: Any) -> Invocation:
        """Invoke an action, returning the thread where it's running"""
        thread = Invocation(action, thing, input)
        self.append_invocation(thread)
        thread.start()
        return thread
    
    def list_invocations(
            self, 
            action: Optional[ActionDescriptor] = None, 
            thing: Optional[Thing] = None,
            as_responses: bool = False,
            request: Optional[Request] = None) -> list[InvocationModel]:
        return [
            i.response(request=request) if as_responses else i 
            for i in self.invocations
            if thing is None or i.thing == thing
            if action is None or i.action==action
        ]
    
    def attach_to_app(self, app: FastAPI):
        """Add /action_invocations and /action_invocation/{id} endpoints to FastAPI"""
        @app.get(ACTION_INVOCATIONS_PATH, response_model=list[InvocationModel])
        def list_all_invocations(request: Request):
            return self.list_invocations(as_responses=True, request=request)
        @app.get(
            ACTION_INVOCATIONS_PATH + "/{id}", 
            response_model=InvocationModel,
            responses={404: {"description": "Invocation ID not found"}}
        )
        def action_invocation(id: uuid.UUID, request: Request):
            try:
                with self._invocations_lock:
                    return self._invocations[id].response(request=request)
            except KeyError:
                raise HTTPException(
                    status_code=404,
                    detail="No action invocation found with ID {id}",
                )
        