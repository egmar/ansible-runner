import os
import stat
import time
import json
import errno
import signal
import codecs
import collections

import pexpect
import psutil

from .utils import OutputEventFilter
from .exceptions import CallbackError, AnsibleRunnerException


class Runner(object):

    def __init__(self, config, cancel_callback=None, remove_partials=True):
        self.config = config
        self.cancel_callback = cancel_callback
        self.canceled = False
        self.timed_out = False
        self.errored = False
        self.status = "unstarted"
        self.rc = None
        self.remove_partials = remove_partials

    def event_callback(self, event_data):
        if 'uuid' in event_data:
            filename = '{}-partial.json'.format(event_data['uuid'])
            partial_filename = os.path.join(self.config.artifact_dir,
                                            'job_events',
                                            filename)
            full_filename = os.path.join(self.config.artifact_dir,
                                         'job_events',
                                         '{}-{}.json'.format(event_data['counter'],
                                                             event_data['uuid']))
            try:
                with codecs.open(partial_filename, 'r', encoding='utf-8') as read_file:
                    partial_event_data = json.load(read_file)
                event_data.update(partial_event_data)
                with codecs.open(full_filename, 'w', encoding='utf-8') as write_file:
                    json.dump(event_data, write_file)
                if self.remove_partials:
                    os.remove(partial_filename)
            except IOError as e:
                print("Failed writing event data: {}".format(e))

    def run(self):
        self.status = "starting"
        stdout_filename = os.path.join(self.config.artifact_dir, 'stdout')

        try:
            os.makedirs(self.config.artifact_dir)
            os.mknod(stdout_filename, stat.S_IFREG | stat.S_IRUSR | stat.S_IWUSR)
        except OSError as exc:
            if exc.errno == errno.EEXIST and os.path.isdir(self.config.artifact_dir):
                pass
            else:
                raise

        stdout_handle = codecs.open(stdout_filename, 'w', encoding='utf-8')
        stdout_handle = OutputEventFilter(stdout_handle, self.event_callback)

        if not isinstance(self.config.expect_passwords, collections.OrderedDict):
            # We iterate over `expect_passwords.keys()` and
            # `expect_passwords.values()` separately to map matched inputs to
            # patterns and choose the proper string to send to the subprocess;
            # enforce usage of an OrderedDict so that the ordering of elements in
            # `keys()` matches `values()`.
            expect_passwords = collections.OrderedDict(self.config.expect_passwords)
        password_patterns = expect_passwords.keys()
        password_values = expect_passwords.values()

        self.status = 'running'
        child = pexpect.spawn(
            self.config.command[0],
            self.config.command[1:],
            cwd=self.config.cwd,
            env=self.config.env,
            ignore_sighup=True,
            encoding='utf-8',
            echo=False,
        )
        child.logfile_read = stdout_handle
        last_stdout_update = time.time()

        job_start = time.time()
        while child.isalive():
            result_id = child.expect(password_patterns,
                                     timeout=self.config.pexpect_timeout,
                                     searchwindowsize=100)
            password = password_values[result_id]
            if password is not None:
                child.sendline(password)
                last_stdout_update = time.time()
            if self.cancel_callback:
                try:
                    self.canceled = self.cancel_callback()
                except Exception:
                    # TODO: logger.exception('Could not check cancel callback - cancelling immediately')
                    #if isinstance(extra_update_fields, dict):
                    #    extra_update_fields['job_explanation'] = "System error during job execution, check system logs"
                    raise CallbackError("Exception in Cancel Callback")
            if not self.canceled and self.config.job_timeout != 0 and (time.time() - job_start) > self.config.job_timeout:
                self.timed_out = True
                # if isinstance(extra_update_fields, dict):
                #     extra_update_fields['job_explanation'] = "Job terminated due to timeout"
            if self.canceled or self.timed_out or self.errored:
                # TODO: proot_cmd
                Runner.handle_termination(child.pid, child.args, proot_cmd=None, is_cancel=self.canceled)
            if self.config.idle_timeout and (time.time() - last_stdout_update) > self.config.idle_timeout:
                child.close(True)
                self.canceled = True

        if self.canceled:
            self.status = "canceled"
        elif child.exitstatus == 0 and not self.timed_out:
            self.status = "successful"
        else:
            self.status = "failed"
        self.rc = child.exitstatus

        for filename, data in [
            ('status', self.status),
            ('rc', self.rc),
        ]:
            artifact_path = os.path.join(self.config.artifact_dir, filename)
            if not os.path.exists(artifact_path):
                os.mknod(artifact_path, stat.S_IFREG | stat.S_IRUSR | stat.S_IWUSR)
            with open(artifact_path, 'w') as f:
                f.write(str(data))
        return self.status, self.rc

    @property
    def stdout(self):
        stdout_path = os.path.join(self.config.artifact_dir, 'stdout')
        if not os.path.exists(stdout_path):
            raise AnsibleRunnerException("stdout missing")
        return open(os.path.join(self.config.artifact_dir, 'stdout'), 'r')

    @property
    def events(self):
        event_path = os.path.join(self.config.artifact_dir, 'job_events')
        if not os.path.exists(event_path):
            raise AnsibleRunnerException("events missing")
        dir_events = os.listdir(event_path)
        dir_events.sort(lambda x, y: int(x.split("-", 1)[0]) - int(y.split("-", 1)[0]))
        for event_file in dir_events:
            with codecs.open(os.path.join(event_path, event_file), 'r', encoding='utf-8') as event_file_actual:
                event = json.load(event_file_actual)
            yield event

    @property
    def stats(self):
        last_event = filter(lambda x: 'event' in x and x['event'] == 'playbook_on_stats',
                            self.events)
        if not last_event:
            return None
        last_event = last_event[0]['event_data']
        return dict(skipped=last_event['skipped'],
                    ok=last_event['ok'],
                    dark=last_event['dark'],
                    failures=last_event['failures'],
                    processed=last_event['processed'])

    @classmethod
    def handle_termination(pid, args, proot_cmd, is_cancel=True):
        '''
        Terminate a subprocess spawned by `pexpect`.

        :param pid:       the process id of the running the job.
        :param args:      the args for the job, i.e., ['ansible-playbook', 'abc.yml']
        :param proot_cmd  the command used to isolate processes i.e., `bwrap`
        :param is_cancel: flag showing whether this termination is caused by
                          instance's cancel_flag.
        '''
        try:
            if proot_cmd in ' '.join(args):
                if not psutil:
                    os.kill(pid, signal.SIGKILL)
                else:
                    try:
                        main_proc = psutil.Process(pid=pid)
                        child_procs = main_proc.children(recursive=True)
                        for child_proc in child_procs:
                            os.kill(child_proc.pid, signal.SIGKILL)
                        os.kill(main_proc.pid, signal.SIGKILL)
                    except (TypeError, psutil.Error):
                        os.kill(pid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGTERM)
            time.sleep(3)
        except OSError:
            raise
            #keyword = 'cancel' if is_cancel else 'timeout'
            #TODO: logger.warn("Attempted to %s already finished job, ignoring" % keyword)