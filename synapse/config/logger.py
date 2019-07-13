# -*- coding: utf-8 -*-
# Copyright 2014-2016 OpenMarket Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import logging.config
import os
import sys
from string import Template

import yaml

from twisted.logger import STDLibLogObserver, globalLogBeginner
from twisted.python.filepath import FilePath

import synapse
from synapse.app import _base as appbase
from synapse.logging._structured import (
    reload_structured_logging,
    setup_structured_logging,
)
from synapse.logging.context import LoggingContextFilter
from synapse.util.versionstring import get_version_string

from ._base import Config

DEFAULT_LOG_CONFIG = Template(
    """
version: 1

formatters:
    precise:
        format: '%(asctime)s - %(name)s - %(lineno)d - %(levelname)s - \
%(request)s - %(message)s'

filters:
    context:
        (): synapse.logging.context.LoggingContextFilter
        request: ""

handlers:
    file:
        class: logging.handlers.RotatingFileHandler
        formatter: precise
        filename: ${log_file}
        maxBytes: 104857600
        backupCount: 10
        filters: [context]
        encoding: utf8
    console:
        class: logging.StreamHandler
        formatter: precise
        filters: [context]

loggers:
    synapse:
        level: INFO

    synapse.storage.SQL:
        # beware: increasing this to DEBUG will make synapse log sensitive
        # information such as access tokens.
        level: INFO

root:
    level: INFO
    handlers: [file, console]
"""
)


class LoggingConfig(Config):
    def read_config(self, config, **kwargs):
        self.verbosity = config.get("verbose", 0)
        self.no_redirect_stdio = config.get("no_redirect_stdio", False)
        self.log_config = self.abspath(config.get("log_config"))
        self.log_file = self.abspath(config.get("log_file"))

    def generate_config_section(self, config_dir_path, server_name, **kwargs):
        log_config = os.path.join(config_dir_path, server_name + ".log.config")
        return (
            """\
        ## Logging ##

        # A yaml python logging config file
        #
        log_config: "%(log_config)s"
        """
            % locals()
        )

    def read_arguments(self, args):
        if args.verbose is not None:
            self.verbosity = args.verbose
        if args.no_redirect_stdio is not None:
            self.no_redirect_stdio = args.no_redirect_stdio
        if args.log_config is not None:
            self.log_config = args.log_config
        if args.log_file is not None:
            self.log_file = args.log_file

    def add_arguments(cls, parser):
        logging_group = parser.add_argument_group("logging")
        logging_group.add_argument(
            "-v",
            "--verbose",
            dest="verbose",
            action="count",
            help="The verbosity level. Specify multiple times to increase "
            "verbosity. (Ignored if --log-config is specified.)",
        )
        logging_group.add_argument(
            "-f",
            "--log-file",
            dest="log_file",
            help="File to log to. (Ignored if --log-config is specified.)",
        )
        logging_group.add_argument(
            "--log-config",
            dest="log_config",
            default=None,
            help="Python logging config file",
        )
        logging_group.add_argument(
            "-n",
            "--no-redirect-stdio",
            action="store_true",
            default=None,
            help="Do not redirect stdout/stderr to the log",
        )

    def generate_files(self, config, config_dir_path):
        log_config = config.get("log_config")
        if log_config and not os.path.exists(log_config):
            log_file = self.abspath("homeserver.log")
            print(
                "Generating log config file %s which will log to %s"
                % (log_config, log_file)
            )
            with open(log_config, "w") as log_config_file:
                log_config_file.write(DEFAULT_LOG_CONFIG.substitute(log_file=log_file))


def _setup_stdlib_logging(log_config, log_file):
    """
    Set up Python stdlib logging.
    """
    log_format = (
        "%(asctime)s - %(name)s - %(lineno)d - %(levelname)s - %(request)s"
        " - %(message)s"
    )

    if log_config is None:
        # We don't have a logfile, so fall back to the 'verbosity' param from
        # the config or cmdline. (Note that we generate a log config for new
        # installs, so this will be an unusual case)
        level = logging.INFO
        level_for_storage = logging.INFO
        if config.verbosity:
            level = logging.DEBUG
            if config.verbosity > 1:
                level_for_storage = logging.DEBUG

        logger = logging.getLogger("")
        logger.setLevel(level)

        logging.getLogger("synapse.storage.SQL").setLevel(level_for_storage)

        formatter = logging.Formatter(log_format)
        if log_file:
            # TODO: Customisable file size / backup count
            handler = logging.handlers.RotatingFileHandler(
                log_file, maxBytes=(1000 * 1000 * 100), backupCount=3, encoding="utf8"
            )

            def sighup(*args):
                logger.info("Closing log file due to SIGHUP")
                handler.doRollover()
                logger.info("Opened new log file due to SIGHUP")

            appbase.register_sighup(sighup)
        else:
            handler = logging.StreamHandler()

        handler.setFormatter(formatter)
        handler.addFilter(LoggingContextFilter(request=""))
        logger.addHandler(handler)
    else:
        logging.config.dictConfig(log_config)

    # It's critical to point twisted's internal logging somewhere, otherwise it
    # stacks up and leaks kup to 64K object;
    # see: https://twistedmatrix.com/trac/ticket/8164
    #
    # Routing to the python logging framework could be a performance problem if
    # the handlers blocked for a long time as python.logging is a blocking API
    # see https://twistedmatrix.com/documents/current/core/howto/logger.html
    # filed as https://github.com/matrix-org/synapse/issues/1727
    #
    # However this may not be too much of a problem if we are just writing to a file.
    observer = STDLibLogObserver()

    def _log(event):

        if "log_text" in event:
            if event["log_text"].startswith("DNSDatagramProtocol starting on "):
                return

            if event["log_text"].startswith("(UDP Port "):
                return

            if event["log_text"].startswith("Timing out client"):
                return

        return observer(event)

    globalLogBeginner.beginLoggingTo(
        [_log], redirectStandardIO=not config.no_redirect_stdio
    )
    if not config.no_redirect_stdio:
        print("Redirected stdout/stderr to logs")


def _reload_stdlib_logging(*args, log_config=None):
    if not log_config:
        logger.warn("Reloaded a blank config?")

    logging.config.dictConfig(log_config)


def setup_logging(config, use_worker_options=False):
    """ Set up python logging

    Args:
        config (LoggingConfig | synapse.config.workers.WorkerConfig):
            configuration data

        use_worker_options (bool): True to use 'worker_log_config' and
            'worker_log_file' options instead of 'log_config' and 'log_file'.

        register_sighup (func | None): Function to call to register a
            sighup handler.
    """
    log_config = config.worker_log_config if use_worker_options else config.log_config
    log_file = config.worker_log_file if use_worker_options else config.log_file

    def read_config(*args, callback=None):
        log_config_body = yaml.safe_load(FilePath(log_config).getContent())
        if args:
            logging.info("Reloaded log config from %s due to SIGHUP", log_config)
        if callback:
            callback(log_config=log_config_body)
        return log_config_body

    log_config_body = read_config()

    if log_config_body.get("version") == 2:
        setup_structured_logging(log_config_body)
        appbase.register_sighup(read_config, callback=reload_structured_logging)
    else:
        _setup_stdlib_logging(log_config_body, log_file)
        appbase.register_sighup(read_config, callback=_reload_stdlib_logging)

    # make sure that the first thing we log is a thing we can grep backwards
    # for
    logging.warn("***** STARTING SERVER *****")
    logging.warn("Server %s version %s", sys.argv[0], get_version_string(synapse))
    logging.info("Server hostname: %s", config.server_name)
