#!/usr/bin/env python3
"""Deploys DC/OS AWS CF template and then runs integration_test.py

The following environment variables control test procedure:

DCOS_TEMPLATE_URL: string
    The template to be used for deployment testing

DCOS_STACK_NAME: string
    Instead of providing a template, supply the name (or id) of an already
    existing cluster

DCOS_SSH_KEY_PATH: string
    path for the SSH key to be used with a preexiting cluster.
    Defaults to 'default_ssh_key'

DCOS_ADVANCED_TEMPLATE: boolean (default:false)
    If true, then DCOS_TEMPLATE_URL is a zen template

CI_FLAGS: string (default=None)
    If provided, this string will be passed directly to py.test as in:
    py.test -vv CI_FLAGS integration_test.py

TEST_ADD_ENV_*: string (default=None)
    Any number of environment variables can be passed to integration_test.py if
    prefixed with 'TEST_ADD_ENV_'. The prefix will be removed before passing
"""
import logging
import os
import random
import stat
import string
import sys

import retrying

import test_util.aws
import test_util.test_runner
from gen.calc import calculate_environment_variable
from ssh.ssh_tunnel import SSHTunnel

LOGGING_FORMAT = '[%(asctime)s|%(name)s|%(levelname)s]: %(message)s'
logging.basicConfig(format=LOGGING_FORMAT, level=logging.DEBUG)
log = logging.getLogger(__name__)


def check_environment():
    """Test uses environment variables to play nicely with TeamCity config templates
    Grab all the environment variables here to avoid setting params all over

    Returns:
        object: generic object used for cleanly passing options through the test

    Raises:
        AssertionError: if any environment variables or resources are missing
            or do not conform
    """
    options = type('Options', (object,), {})()

    # Defaults
    options.ci_flags = os.getenv('CI_FLAGS', '')
    options.aws_region = os.getenv('DEFAULT_AWS_REGION', 'eu-central-1')
    options.advanced = os.getenv('DCOS_ADVANCED_TEMPLATE', 'false') == 'true'
    options.gateway = os.getenv('DCOS_ADVANCED_GATEWAY', None)
    options.vpc = os.getenv('DCOS_ADVANCED_VPC', None)
    options.private_subnet = os.getenv('DCOS_ADVANCED_PRIVATE_SUBNET', None)
    options.public_subnet = os.getenv('DCOS_ADVANCED_PUBLIC_SUBNET', None)
    options.ssh_user = os.getenv('DCOS_SSH_USER', 'core')

    # Mandatory
    options.stack_name = os.getenv('DCOS_STACK_NAME', None)
    options.ssh_key_path = os.getenv('DCOS_SSH_KEY_PATH', 'default_ssh_key')
    options.template_url = os.getenv('DCOS_TEMPLATE_URL', None)
    if not options.template_url:
        assert options.stack_name is not None, 'if DCOS_TEMPLATE_URL is not provided, '\
            'then DCOS_STACK_NAME must be specified'
        advanced = os.getenv('DCOS_ADVANCED_TEMPLATE', None)
        assert advanced is not None, 'if using DCOS_STACK_NAME, '\
            'then DCOS_ADVANCED_TEMPLATE=[true/false] must be specified'
        options.advanced = advanced == 'true'
    else:
        options.advanced = not options.template_url.endswith('single-master.cloudformation.json') and \
            not options.template_url.endswith('multi-master.cloudformation.json')
    options.aws_access_key_id = calculate_environment_variable('AWS_ACCESS_KEY_ID')
    options.aws_secret_access_key = calculate_environment_variable('AWS_SECRET_ACCESS_KEY')

    add_env = {}
    prefix = 'TEST_ADD_ENV_'
    for k, v in os.environ.items():
        if k.startswith(prefix):
            add_env[k.replace(prefix, '')] = v
    options.add_env = add_env
    options.pytest_dir = os.getenv('DCOS_PYTEST_DIR', '/opt/mesosphere/active/dcos-integration-test')
    options.pytest_cmd = os.getenv('DCOS_PYTEST_CMD', 'py.test -vv -rs ' + options.ci_flags)
    return options


def main():
    options = check_environment()
    cf = provide_cluster(options)
    result = run_test(options, cf)
    if result == 0:
        log.info('Test successsful! Deleting CloudFormation...')
        cf.delete()
    else:
        logging.warning('Test exited with an error')
    if options.ci_flags:
        result = 0  # Wipe the return code so that tests can be muted in CI
    sys.exit(result)


def provide_cluster(options):
    bw = test_util.aws.BotoWrapper(
        region=options.aws_region,
        aws_access_key_id=options.aws_access_key_id,
        aws_secret_access_key=options.aws_secret_access_key)
    if not options.stack_name:
        random_id = ''.join(random.choice(string.ascii_uppercase + string.digits) for _ in range(10))
        stack_name = 'CF-integration-test-{}'.format(random_id)
        log.info('Spinning up AWS CloudFormation with ID: {}'.format(stack_name))
        # TODO(mellenburg): use randomly generated keys this key is delivered by CI or user
        if options.advanced:
            cf = test_util.aws.DcosCfAdvanced.create(
                stack_name=stack_name,
                boto_wrapper=bw,
                template_url=options.template_url,
                private_agents=2,
                public_agents=1,
                key_pair_name='default',
                private_agent_type='m3.xlarge',
                public_agent_type='m3.xlarge',
                master_type='m3.xlarge',
                vpc=options.vpc,
                gateway=options.gateway,
                private_subnet=options.private_subnet,
                public_subnet=options.public_subnet)
        else:
            cf = test_util.aws.DcosCfSimple.create(
                stack_name=stack_name,
                template_url=options.template_url,
                private_agents=2,
                public_agents=1,
                admin_location='0.0.0.0/0',
                key_pair_name='default',
                boto_wrapper=bw)
        cf.wait_for_stack_creation()
    else:
        cf = test_util.aws.DcosCfSimple(options.stack_name, bw)
    return cf


def run_test(options, cf):
    # key must be chmod 600 for test_runner to use
    os.chmod(options.ssh_key_path, stat.S_IREAD | stat.S_IWRITE)

    # Create custom SSH Runnner to help orchestrate the test
    remote_dir = '/home/{}'.format(options.ssh_user)

    master_ips = cf.get_master_ips()
    public_agent_ips = cf.get_public_agent_ips()
    private_agent_ips = cf.get_private_agent_ips()
    test_host = master_ips[0].public_ip
    log.info('Running integration test from: ' + test_host)
    master_list = [i.private_ip for i in master_ips]
    log.info('Master private IPs: ' + repr(master_list))
    agent_list = [i.private_ip for i in private_agent_ips]
    log.info('Private agent private IPs: ' + repr(agent_list))
    public_agent_list = [i.private_ip for i in public_agent_ips]
    log.info('Public agent private IPs: ' + repr(public_agent_list))

    log.info('To access this cluster, use the Mesosphere default shared AWS key '
             '(https://mesosphere.onelogin.com/notes/16670) and SSH with:\n'
             'ssh -i default_ssh_key {}@{}'.format(options.ssh_user, test_host))

    @retrying.retry(wait_fixed=2000, stop_max_delay=120 * 1000)
    def establish_host_connectivity():
        """CF SSH-agent might not be ready, so give it a few tries
        """
        return SSHTunnel(options.ssh_user, options.ssh_key_path, test_host)

    with establish_host_connectivity() as test_host_tunnel:
        return test_util.test_runner.integration_test(
            tunnel=test_host_tunnel,
            test_dir=remote_dir,
            region=options.aws_region,
            dcos_dns=master_list[0],
            master_list=master_list,
            agent_list=agent_list,
            public_agent_list=public_agent_list,
            provider='aws',
            test_dns_search=False,
            aws_access_key_id=options.aws_access_key_id,
            aws_secret_access_key=options.aws_secret_access_key,
            add_env=options.add_env,
            pytest_dir=options.pytest_dir,
            pytest_cmd=options.pytest_cmd)


if __name__ == '__main__':
    main()
