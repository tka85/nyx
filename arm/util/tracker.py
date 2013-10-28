"""
Background tasks for gathering informatino about the tor process.

::

  get_connection_tracker - provides a ConnectionTracker for our tor process
  get_resource_tracker - provides a ResourceTracker for our tor process

  Daemon - common parent for resolvers
    |- run_counter - number of successful runs
    |- get_rate - provides the rate at which we run
    |- set_rate - sets the rate at which we run
    |- set_paused - pauses or continues work
    +- stop - stops further work by the daemon

  ConnectionTracker - periodically checks the connections established by tor
    |- get_custom_resolver - provide the custom conntion resolver we're using
    |- set_custom_resolver - overwrites automatic resolver selecion with a custom resolver
    +- get_connections - provides our latest connection results

  ResourceTracker - periodically checks the resource usage of tor
    |- get_resource_usage - provides our latest resource usage results
    +- last_query_failed - checks if we failed to fetch newer results
"""

import collections
import time
import threading

import arm.util.torTools

from stem.control import State
from stem.util import conf, connection, log, proc, str_tools, system

CONFIG = conf.config_dict('arm', {
  'queries.resourceUsage.rate': 5,
  'queries.connections.minRate': 5,
  'msg.unable_to_use_resolver': '',
  'msg.unable_to_use_all_resolvers': '',
})

CONNECTION_TRACKER = None
RESOURCE_TRACKER = None

# Process resources we poll...
#
#  cpu_sample - average cpu usage since we last checked
#  cpu_average - average cpu usage since we first started tracking the process
#  memory_bytes - memory usage of the process in bytes
#  memory_precent - percentage of our memory used by this process

Resources = collections.namedtuple('Resources', [
  'cpu_sample',
  'cpu_average',
  'memory_bytes',
  'memory_percent',
])

def get_connection_tracker():
  """
  Singleton for tracking the connections established by tor.
  """

  global CONNECTION_TRACKER

  if CONNECTION_TRACKER is None:
    CONNECTION_TRACKER = ConnectionTracker()

  return CONNECTION_TRACKER


def get_resource_tracker():
  """
  Singleton for tracking the resource usage of our tor process.
  """

  global RESOURCE_TRACKER

  if RESOURCE_TRACKER is None:
    RESOURCE_TRACKER = ResourceTracker()

  return RESOURCE_TRACKER


class Daemon(threading.Thread):
  """
  Daemon that can perform a unit of work at a given rate.
  """

  def __init__(self, rate):
    threading.Thread.__init__(self)
    self.setDaemon(True)

    self._daemon_lock = threading.RLock()
    self._process_name = None
    self._process_pid = None

    self._rate = rate
    self._last_ran = -1  # time when we last ran
    self._run_counter = 0  # counter for the number of successful runs

    self._is_paused = False
    self._pause_condition = threading.Condition()
    self._halt = False  # terminates thread if true

    controller = arm.util.torTools.getConn().controller
    controller.add_status_listener(self._tor_status_listener)
    self._tor_status_listener(controller, State.INIT, None)

  def run(self):
    while not self._halt:
      time_since_last_ran = time.time() - self._last_ran

      if self._is_paused or time_since_last_ran < self._rate:
        sleep_duration = max(0.2, self._rate - time_since_last_ran)

        with self._pause_condition:
          if not self._halt:
            self._pause_condition.wait(sleep_duration)

        continue  # done waiting, try again

      with self._daemon_lock:
        if self._process_pid is not None:
          is_successful = self._task(self._process_pid, self._process_name)
        else:
          is_successful = False

        if is_successful:
          self._run_counter += 1

      self._last_ran = time.time()

  def _task(self, process_pid, process_name):
    """
    Task the resolver is meant to perform. This should be implemented by
    subclasses.

    :param int process_pid: pid of the process we're tracking
    :param str process_name: name of the process we're tracking
    """

    pass

  def run_counter(self):
    """
    Provides the number of successful runs so far. This can be used to
    determine if the daemon's results are new for the caller or not.
    """

    return self._run_counter

  def get_rate(self):
    """
    Provides the rate at which we perform our given task.

    :returns: **float** for the rate in seconds at which we perform our task
    """

    return self._rate

  def set_rate(self, rate):
    """
    Sets the rate at which we perform our task in seconds.

    :param float rate: rate at which to perform work in seconds
    """

    self._rate = rate

  def set_paused(self, pause):
    """
    Either resumes or holds off on doing further work.

    :param bool pause: halts work if **True**, resumes otherwise
    """

    self._is_paused = pause

  def stop(self):
    """
    Halts further work and terminates the thread.
    """

    with self._pause_condition:
      self._halt = True
      self._pause_condition.notifyAll()

  def _tor_status_listener(self, controller, event_type, _):
    with self._daemon_lock:
      if not self._halt and event_type in (State.INIT, State.RESET):
        tor_pid = controller.get_pid(None)
        tor_cmd = system.get_name_by_pid(tor_pid) if tor_pid else None

        if tor_cmd is None:
          tor_cmd = 'tor'

        self._process_name = tor_cmd
        self._process_pid = tor_pid


class ConnectionTracker(Daemon):
  """
  Periodically retrieves the connections established by tor.
  """

  def __init__(self):
    Daemon.__init__(self, CONFIG["queries.connections.minRate"])

    self._resolvers = connection.get_system_resolvers()
    self._connections = []
    self._custom_resolver = None

    # Number of times in a row we've either failed with our current resolver or
    # concluded that our rate is too low.

    self._failure_count = 0
    self._rate_too_low_count = 0

  def _task(self, process_pid, process_name):
    if self._custom_resolver:
      resolver = self._custom_resolver
      is_default_resolver = False
    elif self._resolvers:
      resolver = self._resolvers[0]
      is_default_resolver = True
    else:
      return  # nothing to resolve with

    try:
      start_time = time.time()

      self._connections = connection.get_connections(
        resolver,
        process_pid = process_pid,
        process_name = process_name,
      )

      runtime = time.time() - start_time

      if is_default_resolver:
        self._failure_count = 0

      # Reduce our rate if connection resolution is taking a long time. This is
      # most often an issue for extremely busy relays.

      min_rate = 100 * runtime

      if self.get_rate() < min_rate:
        self._rate_too_low_count += 1

        if self._rate_too_low_count >= 3:
          min_rate += 1  # little extra padding so we don't frequently update this
          self.set_rate(min_rate)
          self._rate_too_low_count = 0
          log.debug("connection lookup time increasing to %0.1f seconds per call" % min_rate)
      else:
        self._rate_too_low_count = 0

      return True
    except IOError as exc:
      log.info(exc)

      # Fail over to another resolver if we've repeatedly been unable to use
      # this one.

      if is_default_resolver:
        self._failure_count += 1

        if self._failure_count >= 3:
          self._resolvers.pop()
          self._failure_count = 0

          if self._resolvers:
            log.notice(CONFIG['msg.unable_to_use_resolver'].format(
              old_resolver = resolver,
              new_resolver = self._resolvers[0],
            ))
          else:
            log.notice(CONFIG['msg.unable_to_use_all_resolvers'])

      return False

  def get_custom_resolver(self):
    """
    Provides the custom resolver the user has selected. This is **None** if
    we're picking resolvers dynamically.

    :returns: :data:`stem.util.connection.Resolver` we're overwritten to use
    """

    return self._custom_resolver

  def set_custom_resolver(self, resolver):
    """
    Sets the resolver used for connection resolution. If **None** then this is
    automatically determined based on what is available.

    :param stem.util.connection.Resolver resolver: resolver to use
    """

    self._custom_resolver = resolver

  def get_connections(self):
    """
    Provides the last queried connection results, an empty list if resolver
    has been stopped.

    :returns: **list** of :class:`~stem.util.connection.Connection` we last retrieved
    """

    if self._halt:
      return []
    else:
      return list(self._connections)

class ResourceTracker(Daemon):
  """
  Periodically retrieves the resource usage of tor.
  """

  def __init__(self):
    Daemon.__init__(self, CONFIG["queries.resourceUsage.rate"])

    self._last_sample = None

    # resolves usage via proc results if true, ps otherwise
    self._use_proc = proc.is_available()

    # used to get the deltas when querying cpu time
    self._last_cpu_total = 0

    self.last_lookup = -1

    # sequential times we've failed with this method of resolution
    self._failure_count = 0

  def get_resource_usage(self):
    """
    Provides the last cached resource usage as a named tuple of the form:
    (cpuUsage_sampling, cpuUsage_avg, memUsage_bytes, memUsage_percent)
    """

    with self._daemon_lock:
      return self._last_sample if self._last_sample else Resources(0.0, 0.0, 0, 0.0)

  def last_query_failed(self):
    """
    Provides true if, since we fetched the currently cached results, we've
    failed to get new results. False otherwise.
    """

    return self._failure_count != 0

  def _task(self, process_pid, process_name):
    if process_pid is None:
      return

    time_since_reset = time.time() - self.last_lookup
    new_values = {}

    try:
      if self._use_proc:
        utime, stime, start_time = proc.get_stats(process_pid, proc.Stat.CPU_UTIME, proc.Stat.CPU_STIME, proc.Stat.START_TIME)
        total_cpu_time = float(utime) + float(stime)
        cpu_delta = total_cpu_time - self._last_cpu_total
        new_values["cpuSampling"] = cpu_delta / time_since_reset
        new_values["cpuAvg"] = total_cpu_time / (time.time() - float(start_time))
        new_values["_lastCpuTotal"] = total_cpu_time

        mem_usage = int(proc.get_memory_usage(process_pid)[0])
        total_memory = proc.get_physical_memory()
        new_values["memUsage"] = mem_usage
        new_values["memUsagePercentage"] = float(mem_usage) / total_memory
      else:
        # the ps call formats results as:
        #
        #     TIME     ELAPSED   RSS %MEM
        # 3-08:06:32 21-00:00:12 121844 23.5
        #
        # or if Tor has only recently been started:
        #
        #     TIME      ELAPSED    RSS %MEM
        #  0:04.40        37:57  18772  0.9

        ps_call = system.call("ps -p %s -o cputime,etime,rss,%%mem" % process_pid)

        is_successful = False
        if ps_call and len(ps_call) >= 2:
          stats = ps_call[1].strip().split()

          if len(stats) == 4:
            try:
              total_cpu_time = str_tools.parse_short_time_label(stats[0])
              uptime = str_tools.parse_short_time_label(stats[1])
              cpu_delta = total_cpu_time - self._last_cpu_total
              new_values["cpuSampling"] = cpu_delta / time_since_reset
              new_values["cpuAvg"] = total_cpu_time / uptime
              new_values["_lastCpuTotal"] = total_cpu_time

              new_values["memUsage"] = int(stats[2]) * 1024 # ps size is in kb
              new_values["memUsagePercentage"] = float(stats[3]) / 100.0
              is_successful = True
            except ValueError, exc: pass

        if not is_successful:
          raise IOError("unrecognized output from ps: %s" % ps_call)
    except IOError, exc:
      new_values = {}
      self._failure_count += 1

      if self._use_proc:
        if self._failure_count >= 3:
          # We've failed three times resolving via proc. Warn, and fall back
          # to ps resolutions.
          log.info("Failed three attempts to get process resource usage from proc, falling back to ps (%s)" % exc)

          self._use_proc = False
          self._failure_count = 1 # prevents last_query_failed() from thinking that we succeeded
        else:
          # wait a bit and try again
          log.debug("Unable to query process resource usage from proc (%s)" % exc)
      else:
        # exponential backoff on making failed ps calls
        sleep_time = 0.01 * (2 ** self._failure_count) + self._failure_count
        log.debug("Unable to query process resource usage from ps, waiting %0.2f seconds (%s)" % (sleep_time, exc))

    # sets the new values
    if new_values:
      # If this is the first run then the cpuSampling stat is meaningless
      # (there isn't a previous tick to sample from so it's zero at this
      # point). Setting it to the average, which is a fairer estimate.
      if self.last_lookup == -1:
        new_values["cpuSampling"] = new_values["cpuAvg"]

      with self.val_lock:
        self._last_sample = Resources(new_values["cpuSampling"], new_values["cpuAvg"], new_values["memUsage"], new_values["memUsagePercentage"])
        self._last_cpu_total = new_values["_lastCpuTotal"]
        self.last_lookup = time.time()
        self._failure_count = 0
        return True
    else:
      return False
