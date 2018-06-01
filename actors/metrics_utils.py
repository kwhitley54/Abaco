import requests
import json
import datetime

from models import dict_to_camel, Actor, Execution, ExecutionsSummary, Nonce, Worker, get_permissions, \
    set_permission
from worker import shutdown_workers, shutdown_worker
from stores import actors_store, executions_store, logs_store, nonce_store, permissions_store
from prometheus_client import start_http_server, Summary, MetricsHandler, Counter, Gauge, generate_latest
from channels import ActorMsgChannel, CommandChannel, ExecutionResultsChannel
from agaveflask.logs import get_logger
logger = get_logger(__name__)

message_gauges = {}
PROMETHEUS_URL = 'http://172.17.0.1:9090'


def create_gauges(actor_ids):
    for actor_id in actor_ids:
        if actor_id not in message_gauges.keys():
            try:
                g = Gauge(
                    'message_count_for_actor_{}'.format(actor_id.decode("utf-8").replace('-', '_')),
                    'Number of messages for actor {}'.format(actor_id.decode("utf-8").replace('-', '_'))
                )
                message_gauges.update({actor_id: g})
            except Exception as e:
                logger.info("got exception trying to instantiate the Gauge: {}".format(e))
        else:
            g = message_gauges[actor_id]

        try:
            ch = ActorMsgChannel(actor_id=actor_id.decode("utf-8"))
        except Exception as e:
            logger.error("Exception connecting to ActorMsgChannel: {}".format(e))
            raise e
        result = {'messages': len(ch._queue._queue)}
        ch.close()
        g.set(result['messages'])
        logger.debug("METRICS: {} messages found for actor: {}.".format(result['messages'], actor_id))


def query_message_count_for_actor(actor_id):
    query = {
        'query': 'message_count_for_actor_{}'.format(actor_id.decode("utf-8").replace('-', '_')),
        'time': datetime.datetime.utcnow().isoformat() + "Z"
    }
    r = requests.get(PROMETHEUS_URL + '/api/v1/query', params=query)
    data = json.loads(r.text)['data']['result']
    logger.debug('DATA: {}'.format(data))


def calc_change_rate(data, last_metric, actor_id):
    change_rate = 0
    try:
        previous_data = last_metric[actor_id]
        previous_message_count = int(previous_data[0]['value'][1])
        try:
            # what is data?
            current_message_count = int(data[0]['value'][1])
            change_rate = current_message_count - previous_message_count
        except:
            logger.debug("Could not calculate change rate.")
    except:
        logger.info("No previous data yet for new actor {}".format(actor_id))


def scale_up(actor_id):
    tenant, aid = actor_id.decode('utf8').split('_')
    logger.debug('METRICS Attempting to create a new worker for {}'.format(actor_id))
    try:
        # create a worker & add to this actor
        actor = Actor.from_db(actors_store[actor_id])
        worker_ids = [Worker.request_worker(tenant=tenant, actor_id=aid)]
        logger.info("New worker id: {}".format(worker_ids[0]))
        ch = CommandChannel()
        ch.put_cmd(actor_id=actor.db_id,
                   worker_ids=worker_ids,
                   image=actor.image,
                   tenant=tenant,
                   num=1,
                   stop_existing=False)
        ch.close()
        logger.debug('METRICS Added worker successfully for {}'.format(actor_id))
    except Exception as e:
        logger.debug("METRICS - SOMETHING BROKE: {} - {} - {}".format(type(e), e, e.args))


def scale_down(actor_id):
    workers = Worker.get_workers(actor_id)
    logger.debug('METRICS NUMBER OF WORKERS: {}'.format(len(workers)))
    try:
        if len(workers) == 1:
            logger.debug("METRICS only one worker, won't scale down")
        else:
            while len(workers) > 0:
                logger.debug('METRICS made it STATUS check')
                worker = workers.popitem()[1]
                logger.debug('METRICS SCALE DOWN current worker: {}'.format(worker['status']))
                # check status of the worker is ready
                if worker['status'] == 'READY':
                    logger.debug("METRICS I MADE IT")
                    # scale down
                    try:
                        shutdown_worker(worker['id'])
                        continue
                    except Exception as e:
                        logger.debug('METRICS ERROR shutting down worker: {} - {} - {}'.format(type(e), e, e.args))
                    logger.debug('METRICS shut down worker {}'.format(worker['id']))

    except IndexError:
        logger.debug('METRICS only one worker found for actor {}. '
                     'Will not scale down'.format(actor_id))
    except Exception as e:
        logger.debug("METRICS SCALE UP FAILED: {}".format(e))
