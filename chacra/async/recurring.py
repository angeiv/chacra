import datetime
import errno
import os
import json
import pecan
import requests
import shutil
from celery import shared_task
from chacra import models
from chacra.async import base, debian, rpm, post_queued, post_deleted
import logging

logger = logging.getLogger(__name__)


@shared_task(base=base.SQLATask)
def poll_repos():
    """
    Poll the repository objects that need to be updated and call the tasks
    that can create (or update) repositories with that information

    """
    logger.info('polling repos....')
    for r in models.Repo.query.filter_by(needs_update=True, is_queued=False).all():
        # this repo is being processed, do not pile up and try to get it
        # processed again until it is done doing work
        if r.is_updating:
            continue
        if r.needs_update:
            logger.info("repo %s needs to be updated/created", r)
            r.is_queued = True
            post_queued(r)
            if r.type == 'rpm':
                rpm.create_rpm_repo.apply_async(
                    (r.id,),
                    countdown=pecan.conf.quiet_time,
                    queue='build_repos',
                    )
            elif r.type == 'deb':
                debian.create_deb_repo.apply_async(
                    (r.id,),
                    countdown=pecan.conf.quiet_time,
                    queue='build_repos',
                    )
            else:
                _type = r.infer_type()
                if _type is None:
                    logger.warning('failed to infer repository type')
                    logger.warning('got a repository with an unknown type: %s', r)
                else:
                    logger.warning('inferred repo type as: %s', _type)
                    r.type = _type

            models.commit()

    logger.info('completed repo polling')


@shared_task(base=base.SQLATask)
def purge_repos(_now=None):
    """
    Purge built repositories, including the associated model objects.
    """
    if getattr(pecan.conf, 'purge_repos', False) is False:
        logger.info('purge_repos option is unset or explicitly disabled, will skip purge')
        return

    logger.info('polling repos for purging....')
    now = _now or datetime.datetime.utcnow()

    # XXX Magical datetime, not user configurable
    lifespan = now - datetime.timedelta(weeks=2)

    for r in models.Repo.query.filter(models.Repo.modified < lifespan).all():
        logger.info('repo %s is being processed for removal', r)
        for b in r.binaries:
            try:
                os.remove(b.path)
            except OSError as err:
                # no such file, ignore
                if err.errno == errno.ENOENT:
                    pass
            b.delete()
            models.flush()
        try:
            if r.path:
                shutil.rmtree(r.path)
        except OSError as err:
            # no such file, ignore
            if err.errno == errno.ENOENT:
                pass
            raise
        post_deleted(r)
        r.delete()
        models.commit()
    logger.info('completed repo purging')


@shared_task(acks_late=True, bind=True, default_retry_delay=30)
def callback(self, data, project_name, url=None):
    """
    Send a callback to a remote HTTP service. Useful in cases where it is
    needed to advertise the current state of building repositories (since it is
    a time consuming process).

    ``data`` Can be an encoded JSON string or a dictionary. If using specialty
    fields like datetime values, it is safer to use `pecan.jsonify.encode` to
    serialize properly.

    ``acks_late`` will wait until after the task has been acknowledged (not
    before, which is the default) giving a more robust behavior along with
    retrying.

    There is no ``max_retries`` explicitly set because the default (retry
    3 times) is good enough.

    ``default_retry_delay`` is changed to 30 seconds. The default is 3 minutes
    which is too long for this callback.

    .. note:: The use of ``self`` is odd for a function, but this is achieved
    byt the ``bind=True`` so that this task gets access to the task type
    instance

    More detailed information can be found at:

    http://docs.celeryproject.org/en/latest/userguide/tasks.html#retrying
    """
    if url is None:
        if not getattr(pecan.conf, "callback_url", False):
            return
        url = os.path.join(pecan.conf.callback_url, project_name, '')
    headers = {'Content-type': 'application/json'}
    logger.debug('callback for url: %s', url)
    try:
        user = pecan.conf.callback_user
        key = pecan.conf.callback_key
    except AttributeError:
        logger.exception('callback authentication information missing')
        return False

    verify_ssl = getattr(pecan.conf, "callback_verify_ssl", True)

    if isinstance(data, dict):
        try:
            data = json.dumps(data)
        except TypeError:
            logger.exception('could not serialize data')
            return False
    try:
        response = requests.post(
            url,
            data=data,
            auth=(user, key),
            verify=verify_ssl,
            headers=headers
        )
        response.raise_for_status()
    except requests.HTTPError as exc:
        logger.warning('callback failed: %s', str(exc))
        raise self.retry(exc=exc)
    except Exception:
        # Celery eats exceptions for breakfast
        logger.exception('fatal error trying to POST callback')
