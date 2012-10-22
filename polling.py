"""I/O loop implementations based on kqueue() and poll().

If both exist, kqueue() is preferred.  If neither exists, raise
ImportError.

TODO:
- Do we need fd and flags in event tuples?
- Docstrings, unittests.
- Keyword args to callbacks.
"""

import collections
import concurrent.futures
import heapq
import logging
import os
import select
import time


class PollsterBase:

    def __init__(self):
        super().__init__()
        self.readers = {}  # {fd: (callback, args), ...}.
        self.writers = {}  # {fd: (callback, args), ...}.

    def pollable(self):
        return bool(self.readers or self.writers)

    def add_reader(self, fd, callback, *args):
        self.readers[fd] = (callback, args)

    def add_writer(self, fd, callback, *args):
        self.writers[fd] = (callback, args)

    def remove_reader(self, fd):
        del self.readers[fd]

    def remove_writer(self, fd):
        del self.writers[fd]

    def poll(self, timeout=None):
        raise NotImplementedError


class SelectMixin(PollsterBase):

    def poll(self, timeout=None):
        readable, writable, _ = select.select(self.readers, self.writers,
                                              [], timeout)
        events = []
        events += ((fd, 'r') + self.readers[fd] for fd in readable)
        events += ((fd, 'w') + self.writers[fd] for fd in writable)
        return events


class PollMixin(PollsterBase):

    def __init__(self):
        super().__init__()
        self._poll = select.poll()

    def _update(self, fd):
        assert isinstance(fd, int), fd
        flags = 0
        if fd in self.readers:
            flags |= select.POLLIN
        if fd in self.writers:
            flags |= select.POLLOUT
        if flags:
            self._poll.register(fd, flags)
        else:
            self._poll.unregister(fd)

    def add_reader(self, fd, callback, *args):
        super().add_reader(fd, callback, *args)
        self._update(fd)

    def add_writer(self, fd, callback, *args):
        super().add_writer(fd, callback, *args)
        self._update(fd)

    def remove_reader(self, fd):
        super().remove_reader(fd)
        self._update(fd)

    def remove_writer(self, fd):
        super().remove_writer(fd)
        self._update(fd)

    def poll(self, timeout=None):
        # Timeout is in seconds, but poll() takes milliseconds. :-(
        msecs = None if timeout is None else int(round(1000 * timeout))
        events = []
        for fd, flags in self._poll.poll(msecs):
            if flags & (select.POLLIN | select.POLLHUP):
                if fd in self.readers:
                    callback, args = self.readers[fd]
                    events.append((fd, flags, callback, args))
            if flags & (select.POLLOUT | select.POLLHUP):
                if fd in self.writers:
                    callback, args = self.writers[fd]
                    events.append((fd, flags, callback, args))
        return events


class EPollMixin(PollsterBase):

    def __init__(self):
        super().__init__()
        self._epoll = select.epoll()

    def _update(self, fd):
        assert isinstance(fd, int), fd
        eventmask = 0
        if fd in self.readers:
            eventmask |= select.EPOLLIN
        if fd in self.writers:
            eventmask |= select.EPOLLOUT
        if eventmask:
            try:
                self._epoll.register(fd, eventmask)
            except IOError:
                self._epoll.modify(fd, eventmask)
        else:
            self._epoll.unregister(fd)

    def add_reader(self, fd, callback, *args):
        super().add_reader(fd, callback, *args)
        self._update(fd)

    def add_writer(self, fd, callback, *args):
        super().add_writer(fd, callback, *args)
        self._update(fd)

    def remove_reader(self, fd):
        super().remove_reader(fd)
        self._update(fd)

    def remove_writer(self, fd):
        super().remove_writer(fd)
        self._update(fd)

    def poll(self, timeout=None):
        if timeout is None:
            timeout = -1  # epoll.poll() uses -1 to mean "wait forever".
        events = []
        for fd, eventmask in self._epoll.poll(timeout):
            if eventmask & (select.EPOLLIN | select.EPOLLHUP):
                if fd in self.readers:
                    callback, args = self.readers[fd]
                    events.append((fd, eventmask, callback, args))
            if eventmask & (select.EPOLLOUT | select.EPOLLHUP):
                if fd in self.writers:
                    callback, args = self.writers[fd]
                    events.append((fd, eventmask, callback, args))
        return events


class KqueueMixin(PollsterBase):

    def __init__(self):
        super().__init__()
        self._kqueue = select.kqueue()

    def add_reader(self, fd, callback, *args):
        if fd not in self.readers:
            kev = select.kevent(fd, select.KQ_FILTER_READ, select.KQ_EV_ADD)
            self._kqueue.control([kev], 0, 0)
        super().add_reader(fd, callback, *args)

    def add_writer(self, fd, callback, *args):
        if fd not in self.readers:
            kev = select.kevent(fd, select.KQ_FILTER_WRITE, select.KQ_EV_ADD)
            self._kqueue.control([kev], 0, 0)
        super().add_writer(fd, callback, *args)

    def remove_reader(self, fd):
        super().remove_reader(fd)
        kev = select.kevent(fd, select.KQ_FILTER_READ, select.KQ_EV_DELETE)
        self._kqueue.control([kev], 0, 0)

    def remove_writer(self, fd):
        super().remove_writer(fd)
        kev = select.kevent(fd, select.KQ_FILTER_WRITE, select.KQ_EV_DELETE)
        self._kqueue.control([kev], 0, 0)

    def poll(self, timeout=None):
        events = []
        max_ev = len(self.readers) + len(self.writers)
        for kev in self._kqueue.control(None, max_ev, timeout):
            fd = kev.ident
            flag = kev.filter
            if flag == select.KQ_FILTER_READ and fd in self.readers:
                callback, args = self.readers[fd]
                events.append((fd, flag, callback, args))
            elif flag == select.KQ_FILTER_WRITE and fd in self.writers:
                callback, args = self.writers[fd]
                events.append((fd, flag, callback, args))
        return events


class EventLoopMixin(PollsterBase):

    def __init__(self):
        super().__init__()
        self.ready = collections.deque()  # [(callback, args), ...]
        self.scheduled = []  # [(when, callback, args), ...]

    def call_soon(self, callback, *args):
        self.ready.append((callback, args))

    def call_later(self, when, callback, *args):
        # If when is small enough (~11 days), it's a relative time.
        if when < 10000000:
            when += time.time()
        heapq.heappush(self.scheduled, (when, callback, args))

    def run_once(self):
        # TODO: Break each of these into smaller pieces.
        # TODO: Pass in a timeout or deadline or something.
        # TODO: Refactor to separate the callbacks from the readers/writers.

        # This is the only place where callbacks are actually *called*.
        # All other places just add them to ready.
        while self.ready:
            callback, args = self.ready.popleft()
            try:
                callback(*args)
            except Exception:
                logging.exception('Exception in callback %s %r',
                                  callback, args)

        # Inspect the poll queue.
        if self.pollable():
            if self.scheduled:
                when, _, _ = self.scheduled[0]
                timeout = max(0, when - time.time())
            else:
                timeout = None
            events = self.poll(timeout)
            for fd, flag, callback, args in events:
                self.call_soon(callback, *args)

        # Handle 'later' callbacks that are ready.
        while self.scheduled:
            when, _, _ = self.scheduled[0]
            if when > time.time():
                break
            when, callback, args = heapq.heappop(self.scheduled)
            self.call_soon(callback, *args)

    def run(self):
        while self.ready or self.scheduled or self.pollable():
            self.run_once()


# Select most appropriate base class for platform.
if hasattr(select, 'kqueue'):  # Most BSD
    poll_base = KqueueMixin
elif hasattr(select, 'epoll'):  # Linux 2.5 and later
    poll_base = EPollMixin
elif hasattr(select, 'poll'):  # Newer UNIX
    poll_base = PollMixin
else:  # All UNIX; Windows (for sockets only)
    poll_base = SelectMixin


class Pollster(EventLoopMixin, poll_base):
    pass


class ThreadRunner:

    def __init__(self, ioloop, max_workers=5):
        self.ioloop = ioloop
        self.threadpool = concurrent.futures.ThreadPoolExecutor(max_workers)
        self.pipe_read_fd, self.pipe_write_fd = os.pipe()
        self.active_count = 0

    def read_callback(self):
        # Semi-permanent callback while at least one future is active.
        assert self.active_count > 0, self.active_count
        data = os.read(self.pipe_read_fd, 8192)  # Traditional buffer size.
        self.active_count -= len(data)
        if self.active_count == 0:
            self.ioloop.remove_reader(self.pipe_read_fd)
        assert self.active_count >= 0, self.active_count

    def submit(self, func, *args, **kwds):
        assert self.active_count >= 0, self.active_count
        future = self.threadpool.submit(func, *args, **kwds)
        if self.active_count == 0:
            self.ioloop.add_reader(self.pipe_read_fd, self.read_callback)
        self.active_count += 1
        def done_callback(future):
            os.write(self.pipe_write_fd, b'x')
        future.add_done_callback(done_callback)
        return future