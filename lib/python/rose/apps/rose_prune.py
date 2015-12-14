# -*- coding: utf-8 -*-
# ----------------------------------------------------------------------------
# (C) British Crown Copyright 2012-5 Met Office.
#
# This file is part of Rose, a framework for meteorological suites.
#
# Rose is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Rose is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Rose. If not, see <http://www.gnu.org/licenses/>.
# ----------------------------------------------------------------------------
"""Builtin application: rose_prune: suite housekeeping application."""

import os
from rose.app_run import BuiltinApp, ConfigValueError
from rose.date import RoseDateTimeOperator
from rose.env import env_var_process, UnboundEnvironmentVariableError
from rose.fs_util import FileSystemEvent
from rose.host_select import HostSelector
from rose.popen import RosePopenError
import shlex


class RosePruneApp(BuiltinApp):

    """Prune files and directories generated by suite tasks."""

    SCHEME = "rose_prune"
    SECTION = "prune"

    def run(self, app_runner, conf_tree, opts, args, uuid, work_files):
        """Suite housekeeping application.

        This application is designed to work under "rose task-run" in a cycling
        suite.

        """
        suite_name = os.getenv("ROSE_SUITE_NAME")
        if not suite_name:
            return

        # Tar-gzip job logs on suite host
        # Prune job logs on remote hosts and suite host
        prune_remote_logs_cycles = self._get_conf(
            app_runner, conf_tree, "prune-remote-logs-at")
        prune_server_logs_cycles = self._get_conf(
            app_runner, conf_tree, "prune-server-logs-at")
        archive_logs_cycles = self._get_conf(
            app_runner, conf_tree, "archive-logs-at")
        if (prune_remote_logs_cycles or
                prune_server_logs_cycles or
                archive_logs_cycles):
            tmp_prune_remote_logs_cycles = []
            for cycle in prune_remote_logs_cycles:
                if cycle not in archive_logs_cycles:
                    tmp_prune_remote_logs_cycles.append(cycle)
            prune_remote_logs_cycles = tmp_prune_remote_logs_cycles

            tmp_prune_server_logs_cycles = []
            for cycle in prune_server_logs_cycles:
                if cycle not in archive_logs_cycles:
                    tmp_prune_server_logs_cycles.append(cycle)
            prune_server_logs_cycles = tmp_prune_server_logs_cycles

            if prune_remote_logs_cycles:
                app_runner.suite_engine_proc.job_logs_pull_remote(
                    suite_name, prune_remote_logs_cycles,
                    prune_remote_mode=True)

            if prune_server_logs_cycles:
                app_runner.suite_engine_proc.job_logs_remove_on_server(
                    suite_name, prune_server_logs_cycles)

            if archive_logs_cycles:
                app_runner.suite_engine_proc.job_logs_archive(
                    suite_name, archive_logs_cycles)

        # Prune other directories
        globs = self._get_prune_globs(app_runner, conf_tree)
        suite_engine_proc = app_runner.suite_engine_proc
        hosts = suite_engine_proc.get_suite_jobs_auths(suite_name)
        suite_dir_rel = suite_engine_proc.get_suite_dir_rel(suite_name)
        form_dict = {"d": suite_dir_rel, "g": " ".join(globs)}
        sh_cmd_head = r"set -e; cd %(d)s; " % form_dict
        sh_cmd = (
            r"set +e; ls -d %(g)s; " +
            r"set -e; rm -fr %(g)s") % form_dict
        cwd = os.getcwd()
        host_selector = HostSelector(
            app_runner.event_handler, app_runner.popen)
        for host in hosts + [host_selector.get_local_host()]:
            sdir = None
            try:
                if host_selector.is_local_host(host):
                    sdir = suite_engine_proc.get_suite_dir(suite_name)
                    app_runner.fs_util.chdir(sdir)
                    out = app_runner.popen.run_ok(
                        "bash", "-O", "extglob", "-c", sh_cmd)[0]
                else:
                    cmd = app_runner.popen.get_cmd(
                        "ssh", host,
                        "bash -O extglob -c '" + sh_cmd_head + sh_cmd + "'")
                    out = app_runner.popen.run_ok(*cmd)[0]
            except RosePopenError as exc:
                app_runner.handle_event(exc)
            else:
                if sdir is None:
                    event = FileSystemEvent(FileSystemEvent.CHDIR,
                                            host + ":" + suite_dir_rel)
                    app_runner.handle_event(event)
                for line in sorted(out.splitlines()):
                    if not host_selector.is_local_host(host):
                        line = host + ":" + line
                    event = FileSystemEvent(FileSystemEvent.DELETE, line)
                    app_runner.handle_event(event)
            finally:
                if sdir:
                    app_runner.fs_util.chdir(cwd)
        return

    def _get_conf(self, app_runner, conf_tree, key, max_args=0):
        """Get a list of cycles from a configuration setting.

        key -- An option key in self.SECTION to locate the setting.
        max_args -- Maximum number of extra arguments for an item in the list.

        The value of the setting is expected to be split by shlex.split into a
        list of items. If max_args == 0, an item should be a string
        representing a cycle or an cycle offset. If max_args > 0, the cycle
        or cycle offset string can, optionally, have arguments. The arguments
        are delimited by colons ":".
        E.g.:

        prune-remote-logs-at=-PT6H -PT12H
        prune-server-logs-at=-P7D
        prune-datac-at=-PT6H:foo/* -PT12H:'bar/* baz/*' -P1D
        prune-work-at=-PT6H:t1*:*.tar -PT12H:t1*: -PT12H:*.gz -P1D

        If max_args == 0, return a list of cycles.
        If max_args > 0, return a list of (cycle, [arg, ...])

        """
        items_str = conf_tree.node.get_value([self.SECTION, key])
        if items_str is None:
            return []
        try:
            items_str = env_var_process(items_str)
        except UnboundEnvironmentVariableError as exc:
            raise ConfigValueError([self.SECTION, key], items_str, exc)
        items = []
        ref_point_str = os.getenv(
            RoseDateTimeOperator.TASK_CYCLE_TIME_ENV)
        try:
            ref_point = None
            ref_fmt = None
            for item_str in shlex.split(items_str):
                args = item_str.split(":", max_args)
                when = args.pop(0)
                cycle = when
                if ref_point_str is not None:
                    if self._get_cycling_mode() == "integer":
                        # Integer cycling
                        if "P" in when:  # "when" is an offset
                            cycle = str(int(ref_point_str) +
                                        int(when.replace("P", "")))
                        else:  # "when" is a cycle point
                            cycle = str(when)
                    else:
                        # Date-time cycling
                        if ref_fmt is None:
                            ref_point, ref_fmt = (
                                app_runner.date_time_oper.date_parse(
                                    ref_point_str))
                        try:
                            time_point = app_runner.date_time_oper.date_parse(
                                when)[0]
                        except ValueError:
                            time_point = app_runner.date_time_oper.date_shift(
                                ref_point, when)
                        cycle = app_runner.date_time_oper.date_format(
                            ref_fmt, time_point)
                if max_args:
                    items.append((cycle, args))
                else:
                    items.append(cycle)
        except ValueError as exc:
            raise ConfigValueError([self.SECTION, key], items_str, exc)
        return items

    @classmethod
    def _get_cycling_mode(cls):
        """Return task cycling mode."""
        return os.getenv("ROSE_CYCLING_MODE")

    def _get_prune_globs(self, app_runner, conf_tree):
        """Return prune globs."""
        globs = []
        nodes = conf_tree.node.get_value([self.SECTION])
        if nodes is None:
            return []
        cycle_formats = {}
        for key, node in nodes.items():
            if node.is_ignored():
                continue
            if key.startswith("cycle-format{") and key.endswith("}"):
                fmt = key[len("cycle-format{"):-1]
                try:
                    cycle_formats[fmt] = env_var_process(node.value)
                    # Check formats are valid
                    if self._get_cycling_mode() == "integer":
                        cycle_formats[fmt] % 0
                    else:
                        app_runner.date_time_oper.date_format(
                            cycle_formats[fmt])
                except (UnboundEnvironmentVariableError, ValueError) as exc:
                    raise ConfigValueError(
                        [self.SECTION, key], node.value, exc)
        for key, node in sorted(nodes.items()):
            if node.is_ignored():
                continue
            if key == "prune-datac-at":  # backward compat
                head = "share/cycle"
            elif key == "prune-work-at":  # backward compat
                head = "work"
            elif key.startswith("prune{") and key.endswith("}"):
                head = key[len("prune{"):-1].strip()  # remove "prune{" and "}"
            else:
                continue
            for cycle, cycle_args in self._get_conf(
                    app_runner, conf_tree, key, max_args=1):
                if cycle_args:
                    cycle_strs = {"cycle": cycle}
                    for key, cycle_format in cycle_formats.items():
                        if self._get_cycling_mode() == "integer":
                            cycle_strs[key] = cycle_format % int(cycle)
                        else:  # date time cycling
                            cycle_point = (
                                app_runner.date_time_oper.date_parse(cycle)[0])
                            cycle_strs[key] = (
                                app_runner.date_time_oper.date_format(
                                    cycle_format, cycle_point))
                    for tail_glob in shlex.split(cycle_args.pop()):
                        glob_ = tail_glob % cycle_strs
                        if glob_ == tail_glob:  # no substitution
                            glob_ = os.path.join(cycle, tail_glob)
                        globs.append(os.path.join(head, glob_))
                else:
                    globs.append(os.path.join(head, cycle))
        return globs
