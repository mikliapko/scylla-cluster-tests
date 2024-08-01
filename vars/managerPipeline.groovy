#!groovy

boolean jobEnabled (String jobName) {
	echo "Checking if Job $jobName exists / enabled"
	try {
		if (Jenkins.instance.getItemByFullName(jobName).isBuildable()) {
			echo "Job $jobName is enabled"
			return true
		} else {
			echo "Job $jobName is disabled, Skipping"
			return false
		}
	} catch (error) {
		echo "Error: General error |$error| while checking if job |$jobName| enabled (job does not exist)"
		return false
	}
}

def triggerJob(String jobToTrigger, def parameterList = [], boolean propagate = false, boolean wait = false) {
    if (jobEnabled(jobToTrigger)) {
        echo "Triggering '$jobToTrigger'"
        try {
            jobResults=build job: jobToTrigger,
                parameters: parameterList,
                propagate: propagate,  // if true, the triggering test will fail/pass based on the status of the triggered/downstream job/s
                wait: wait  // if true, the triggering job will not end until the triggered/downstream job/s will end
        } catch(Exception ex) {
            echo "Could not trigger jon $jobToTrigger due to"
            println(ex.toString())
        }
    }
}


def completed_stages = [:]
def (testDuration, testRunTimeout, runnerTimeout, collectLogsTimeout, resourceCleanupTimeout) = [0,0,0,0,0]

def call(Map pipelineParams) {

    def builder = getJenkinsLabels(params.backend, params.region, params.gce_datacenter, params.azure_region_name)

    pipeline {
        agent {
            label {
                label builder.label
            }
        }
        environment {
            AWS_ACCESS_KEY_ID     = credentials('qa-aws-secret-key-id')
            AWS_SECRET_ACCESS_KEY = credentials('qa-aws-secret-access-key')
            SCT_TEST_ID = UUID.randomUUID().toString()
        }
        parameters {
            string(defaultValue: "${pipelineParams.get('backup_bucket_backend', '')}",
               description: 's3|gcs|azure or empty',
               name: 'backup_bucket_backend')
            string(defaultValue: "${pipelineParams.get('backend', 'aws')}",
               description: 'aws|gce',
               name: 'backend')
            string(defaultValue: "${pipelineParams.get('region', 'eu-west-1')}",
               description: 'Supported: us-east-1 | eu-west-1 | eu-west-2 | eu-north-1 | eu-central-1 | us-west-2 | random (randomly select region)',
               name: 'region')
            string(defaultValue: "${pipelineParams.get('n_db_nodes', '')}",
               description: 'Number of db nodes. If case of multiDC cluster, use a space-separated string, for example "2 1"',
               name: 'n_db_nodes')
            string(defaultValue: "${pipelineParams.get('gce_datacenter', 'us-east1')}",
                   description: 'GCE datacenter',
                   name: 'gce_datacenter')
            string(defaultValue: "${pipelineParams.get('azure_region_name', 'eastus')}",
                   description: 'Azure location',
                   name: 'azure_region_name')
            string(defaultValue: "a",
               description: 'Availability zone',
               name: 'availability_zone')


            string(defaultValue: '', description: '', name: 'scylla_ami_id')
            string(defaultValue: "${pipelineParams.get('scylla_version', '2024.1')}", description: '', name: 'scylla_version')
            // When branching to manager version branch, set scylla_version to the latest release
            string(defaultValue: '', description: '', name: 'scylla_repo')
            string(defaultValue: "${pipelineParams.get('gce_image_db', '')}",
                   description: "gce image of scylla (since scylla_version doesn't work with gce)",
                   name: 'gce_image_db')  // TODO: remove setting once hydra is able to discover scylla images in gce from scylla_version
            string(defaultValue: "${pipelineParams.get('azure_image_db', '')}",
                   description: '',
                   name: 'azure_image_db')
            string(defaultValue: "${pipelineParams.get('provision_type', 'spot')}",
                   description: 'spot|on_demand|spot_fleet',
                   name: 'provision_type')
            string(defaultValue: "${pipelineParams.get('instance_provision_fallback_on_demand', 'false')}",
                   description: 'true|false',
                   name: 'instance_provision_fallback_on_demand')

            string(defaultValue: "${pipelineParams.get('post_behavior_db_nodes', 'keep-on-failure')}",
                   description: 'keep|keep-on-failure|destroy',
                   name: 'post_behavior_db_nodes')
            string(defaultValue: "${pipelineParams.get('post_behavior_loader_nodes', 'destroy')}",
                   description: 'keep|keep-on-failure|destroy',
                   name: 'post_behavior_loader_nodes')
            string(defaultValue: "${pipelineParams.get('post_behavior_monitor_nodes', 'keep-on-failure')}",
                   description: 'keep|keep-on-failure|destroy',
                   name: 'post_behavior_monitor_nodes')

            string(defaultValue: "${pipelineParams.get('ip_ssh_connections', 'private')}",
                   description: 'private|public|ipv6',
                   name: 'ip_ssh_connections')

            string(defaultValue: "${pipelineParams.get('scylla_mgmt_address', '')}",
                   description: 'If empty - the default manager version will be taken',
                   name: 'scylla_mgmt_address')

            string(defaultValue: "${pipelineParams.get('manager_version', 'master_latest')}",
                   description: 'master_latest|3.2|3.1',
                   name: 'manager_version')

            string(defaultValue: "${pipelineParams.get('target_manager_version', '')}",
                   description: 'master_latest|3.2|3.1',
                   name: 'target_manager_version')

            string(defaultValue: "${pipelineParams.get('scylla_mgmt_agent_address', '')}",
                   description: 'manager agent repo',
                   name: 'scylla_mgmt_agent_address')

            string(defaultValue: "${pipelineParams.get('target_scylla_mgmt_server_address', '')}",
                   description: 'Link to the repository of the manager that will be used as a target of the manager server in the manager upgrade test',
                   name: 'target_scylla_mgmt_server_address')

            string(defaultValue: "${pipelineParams.get('target_scylla_mgmt_agent_address', '')}",
                   description: 'Link to the repository of the manager that will be used as a target of the manager agents in the manager upgrade test',
                   name: 'target_scylla_mgmt_agent_address')

            string(defaultValue: "'qa@scylladb.com','mgmt@scylladb.com'",
                   description: 'email recipients of email report',
                   name: 'email_recipients')

            string(defaultValue: "${pipelineParams.get('scylla_mgmt_pkg', '')}",
                   description: 'Url to the scylla manager packages',
                   name: 'scylla_mgmt_pkg')

            string(defaultValue: "${pipelineParams.get('test_config', '')}",
                   description: 'Test configuration file',
                   name: 'test_config')

            string(defaultValue: "${pipelineParams.get('test_name', '')}",
                   description: 'Name of the test to run',
                   name: 'test_name')

            string(defaultValue: "${pipelineParams.get('mgmt_restore_params', '')}",
                   description: """The dict with restore operation specific parameters: batch_size, parallel.
                                   For example, {'batch_size': 2, 'parallel': 1}""",
                   name: 'mgmt_restore_params')

            string(defaultValue: "${pipelineParams.get('mgmt_agent_backup_config', '')}",
                   description: """Backup general configuration for the agent (scylla-manager-agent.yaml):
                                   checkers, transfers, low_level_retries.
                                   For example, {'checkers': 100, 'transfers': 2, 'low_level_retries': 20}""",
                   name: 'mgmt_agent_backup_config')

            string(defaultValue: "${pipelineParams.get('keyspace_num', '')}",
                   description: 'Number of keyspaces to create. If > 1, cassandra-stress inserts the data into each keyspace',
                   name: 'keyspace_num')

            string(defaultValue: "${pipelineParams.get('downstream_jobs_to_run', '')}",
                   description: 'Comma separated list of downstream jobs to run when the job passes',
                   name: 'downstream_jobs_to_run')
            string(defaultValue: '',
                   description: 'Actual user requesting job start, for automated job builds (e.g. through Argus)',
                   name: 'requested_by_user')
        }
        options {
            timestamps()
            disableConcurrentBuilds()
            buildDiscarder(logRotator(numToKeepStr: '20'))
        }
        stages {
            stage('Checkout') {
                options {
                    timeout(time: 5, unit: 'MINUTES')
                }
                steps {
                    script {
                        completed_stages = [:]
                    }
                    dir('scylla-cluster-tests') {
                        checkout scm
                        checkoutQaInternal(params)
                    }
               }
            }
            stage("Collect log data") {
                steps {
                    catchError(stageResult: 'FAILURE') {
                        script {
                            wrap([$class: 'BuildUser']) {
                                dir('scylla-cluster-tests') {
                                    timeout(time: collectLogsTimeout, unit: 'MINUTES') {
                                        runCollectLogs(params, builder.region)
                                        completed_stages['collect_logs'] = true
                                    }
                                }
                            }
                        }
                    }
                }
            }
            stage('Clean resources') {
                steps {
                    catchError(stageResult: 'FAILURE') {
                        script {
                            wrap([$class: 'BuildUser']) {
                                dir('scylla-cluster-tests') {
                                    timeout(time: resourceCleanupTimeout, unit: 'MINUTES') {
                                        runCleanupResource(params, builder.region)
                                        completed_stages['clean_resources'] = true
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}
