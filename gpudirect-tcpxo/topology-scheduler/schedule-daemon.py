#!/usr/bin/env python

# Copyright 2024 Google Inc. All Rights Reserved.
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

import argparse
from itertools import groupby
import time
import kubernetes
import kubernetes.client
from kubernetes.client.rest import ApiException
from kubernetes.utils.quantity import parse_quantity


def split_pods_based_on_jobs(pods):
  """Splits pending pods into groups based on jobs."""
  return [
      list(job_group)
      for _, job_group in groupby(pods, lambda pod: pod.get('job_name'))
  ]


def sort_jobs_by_time(job):
  """Return the key to be used for sorting jobs which is by creation time."""
  # All the pods in the job should have the same creation time.
  return job[0].get('creation_time')


def pod_sorting_key(pod):
  """Returns key to be used for sorting pods.
  Given that numbers is often suffixed for multi-node deployments,
  here we use a (prefix, number) tuple for the sorting key.
  This means "xxx-pod2" should appear before "xxx-pod10"
  """

  if pod['index'] is not None:
    return int(pod['index'])

  # if the suffix is a number, extract it
  idx = 0
  suffix = ""
  name = pod['name']
  while name[-1 - len(suffix)].isdigit():
    suffix = name[-1 - len(suffix)] + suffix

  if suffix != "":
    idx = int(suffix)

  return (name[:len(name) - len(suffix)], idx)


def node_topology_distance(node1, node2):
  node1_key = node_topology_key(node1)
  node2_key = node_topology_key(node2)
  result = 1000000
  for i in range(len(node1_key)):
    if node1_key[i] != node2_key[i]:
      return result
    result /= 100
  return 0


def node_topology_key(node):
  """Builds a key to be used to sort nodes."""
  node_labels = node['node_labels']

  if (
      'cloud.google.com/gke-placement-group' in node_labels
      and 'topology.gke.io/cluster' in node_labels
      and 'topology.gke.io/rack' in node_labels
      and 'topology.gke.io/host' in node_labels
  ):
    return (
        node_labels['cloud.google.com/gke-placement-group'],
        node_labels['topology.gke.io/cluster'],
        node_labels['topology.gke.io/rack'],
        node_labels['topology.gke.io/host'],
    )

  return ()


def get_pod_used_resources(pod):
  """Get the resources used by this pod"""
  used_cpu = 0
  used_memory = 0
  used_gpu = 0
  if pod.status is None or pod.status.container_statuses is None:
    return used_cpu, used_memory, used_gpu
  for container, container_status in zip(pod.spec.containers, pod.status.container_statuses):
    if container_status.state.terminated is not None:
      # terminated pods don't use resources
      continue
    requests = container.resources.requests or {}
    used_cpu += parse_quantity(requests.get('cpu', 0))
    used_memory += parse_quantity(requests.get('memory', 0))
    used_gpu += int(requests.get('nvidia.com/gpu', 0))
  return used_cpu, used_memory, used_gpu


def get_pods_taint_toleration(pods):
  """Get the taint tolerations of the pods.
  For simplicity, we assume that the pods are homogeneous and
  all have the same tolerations.
  """
  ts = None
  for pod in pods:
    tolerations = pod['spec'].tolerations
    if ts is None:
      ts = tolerations
    else:
      assert(ts == tolerations)
  return ts if ts is not None else []


def find_schedulable_nodes(nodes, pods, tolerated_taints):
  """Finds nodes that can be scheduled."""
  nodes_info = {}

  if tolerated_taints is not None:
    tolerated_taint_dict = {t.key: t for t in tolerated_taints}
  else:
    tolerated_taint_dict = {}

  for node in nodes:
    node_name = node.metadata.name
    node_labels = node.metadata.labels

    if 'cloud.google.com/gke-placement-group' not in node_labels:
      print(
          f'Skipping node {node_name} because it does not have topology'
          ' metadata'
      )
      continue

    skip_node = False
    # check node taints
    if node.spec.taints is not None:
      for t in node.spec.taints:
        if t.key not in tolerated_taint_dict:
          print(f'Skipping node {node_name} because it is tainted with key {t.key}')
          skip_node = True
          break
        else:
          tol = tolerated_taint_dict[t.key]
          if tol.operator == "Equal" and tol.value != t.value:
            print(f'Skipping node {node_name} because it is tainted with key {t.key} with value {t.value}')
            skip_node = True
            break
    # check node status
    if any(condition.type == "Ready" and condition.status != "True" for condition in node.status.conditions):
      print(f'Skipping node {node_name} because it is NotReady')
      skip_node = True
      break

    if skip_node:
      continue

    allocatable = node.status.allocatable

    used_cpu = 0
    used_memory = 0
    used_gpu = 0

    for pod in pods:
      if pod.spec.node_name == node_name:
        cpu, mem, gpu = get_pod_used_resources(pod)
        used_cpu += cpu
        used_memory += mem
        used_gpu += gpu

    free_cpu = parse_quantity(allocatable['cpu']) - used_cpu
    free_memory = parse_quantity(allocatable['memory']) - used_memory
    free_gpu = int(allocatable.get('nvidia.com/gpu', 0)) - used_gpu

    node_info = {
        'name': node_name,
        'cpu': free_cpu,
        'memory': free_memory,
        'gpu': free_gpu,
        'node_labels': node_labels,
    }
    nodes_info[node_name] = node_info

    print(
        f'Node: {node_name}, CPU: {free_cpu}, Memory: {free_memory}, GPU:'
        f' {free_gpu}, Topology: {node_topology_key(node_info)}'
    )

  return nodes_info


def find_pod_gates(pods, prefix):
  """Finds pods with scheduling gates that starts with the prefix"""
  s = set()
  for pod in pods:
    if pod.spec.scheduling_gates:
      for g in pod.spec.scheduling_gates:
        if g.name.startswith(prefix):
          s.add(g.name)
  return s


def find_schedulable_pods(pods, gate_name):
  """Finds pods that can be scheduled."""
  pods_to_schedule = {}

  for pod in pods:
    if pod.spec.scheduling_gates:
      gates = pod.spec.scheduling_gates
      for gate in gates:
        if gate.name == gate_name:
          pod_name = pod.metadata.name
          pod_namespace = pod.metadata.namespace

          pod_index = None
          job_name = None
          if pod.metadata.labels is not None:
            if (
                'batch.kubernetes.io/job-completion-index'
                in pod.metadata.labels
            ):
              pod_index = pod.metadata.labels[
                  'batch.kubernetes.io/job-completion-index'
              ]
            else:
              print('Unable to find index in metadata. Can not queue jobs')

            if 'job-name' in pod.metadata.labels:
              job_name = pod.metadata.labels['job-name']
            else:
              print('Unable to find job_name in metadata. Can not queue jobs')
          else:
            print('No labels on pod to extract job metadata from.')

          creation_time = None
          if pod.metadata.creation_timestamp is not None:
            creation_time = pod.metadata.creation_timestamp
          else:
            print(
                'Unable to find creation_time in metadata. Can not queue jobs'
            )

          used_cpu = 0
          used_memory = 0
          used_gpu = 0

          for container in pod.spec.containers:
            requests = container.resources.requests or {}
            used_cpu += parse_quantity(requests.get('cpu', 0))
            used_memory += parse_quantity(requests.get('memory', 0))
            used_gpu += int(requests.get('nvidia.com/gpu', 0))

          pods_to_schedule[pod_name] = {
              'name': pod_name,
              'namespace': pod_namespace,
              'index': pod_index,
              'cpu': used_cpu,
              'memory': used_memory,
              'gpu': used_gpu,
              'node_selector': pod.spec.node_selector,
              'spec': pod.spec,
              'metadata': pod.metadata,
              'job_name': job_name,
              'creation_time': creation_time
          }

          print(
              f'Found schedulable pod: {pod_namespace}/{pod_name}, CPU:'
              f' {used_cpu}, Memory: {used_memory}, GPU: {used_gpu}'
              f' Index: {pod_index}'
          )

  return pods_to_schedule


def can_schedule(node, pod):
  """Checks if a given pod can be scheduled on a given node."""
  node_selector = pod['node_selector']
  node_labels = node['node_labels']

  if node_selector:
    for key, value in node_selector.items():
      if key not in node_labels or node_labels[key] != value:
        return False

  return (
      node['cpu'] >= pod['cpu']
      and node['memory'] >= pod['memory']
      and node['gpu'] >= pod['gpu']
  )


def schedule_pod_on_node(v1, pod_name, pod_namespace, node, gate_name):
  """Schedules a pod on a given node."""
  try:
    pod = v1.read_namespaced_pod(pod_name, pod_namespace)

    if any(gate.name == gate_name for gate in pod.spec.scheduling_gates):
      new_gates = [
          gate for gate in pod.spec.scheduling_gates if gate.name != gate_name
      ]
      pod.spec.affinity = {
          'nodeAffinity': {
              'requiredDuringSchedulingIgnoredDuringExecution': {
                  'nodeSelectorTerms': [{
                      'matchExpressions': [{
                          'key': 'kubernetes.io/hostname',
                          'operator': 'In',
                          'values': [node['name']],
                      }]
                  }]
              }
          }
      }
      pod.spec.scheduling_gates = new_gates

      v1.replace_namespaced_pod(pod_name, pod_namespace, pod)

      print(
        'Pod %s/%s scheduled on %s with topology %s', pod_namespace, pod_name, node['name'], node_topology_key(node)
      )
  except ApiException as e:
    print(f'Exception when removing scheduling gate: {e}')


def calculate_pods_assignment(sorted_nodes, sorted_pods):
  """Calculates the best assignment for pods."""
  assignment = [-i for i in reversed(range(1, len(sorted_pods) + 1))]
  best_assignment = []
  minimum_distance = 1000000000

  while True:
    all_ok = True
    i = len(assignment) - 1
    while i >= 0 and all_ok:
      assignment[i] += 1
      if assignment[i] == len(sorted_nodes):
        break
      if assignment[i] >= 0 and can_schedule(
          sorted_nodes[assignment[i]], sorted_pods[i]
      ):
        i -= 1
      elif i < len(assignment) - 1 and assignment[i] == assignment[i + 1] - 1:
        all_ok = False
    if assignment[-1] == len(sorted_nodes):
      break
    if all_ok:
      new_distance = 0
      for i in range(1, len(sorted_pods)):
        new_distance += node_topology_distance(
            sorted_nodes[assignment[i]], sorted_nodes[assignment[i - 1]]
        )
      if new_distance < minimum_distance:
        best_assignment = assignment.copy()
        minimum_distance = new_distance

  return best_assignment


def schedule_pod_with_gate(v1, pods, gate):
  pods_to_schedule = find_schedulable_pods(pods, gate)

  nodes = v1.list_node().items
  print(f'Pods to schedule: {len(pods_to_schedule)}')
  jobs = split_pods_based_on_jobs(pods_to_schedule.values())
  sorted_jobs = sorted(jobs, key=sort_jobs_by_time)
  for job in sorted_jobs:
    job_name = job[0].get('job_name')
    creation_time = job[0].get('creation_time')
    print(f'Attempting to schedule job: {job_name} created: {creation_time}')

    tolerated_taints = get_pods_taint_toleration(job)
    nodes_to_schedule = find_schedulable_nodes(nodes, pods, tolerated_taints)

    sorted_pods = sorted(job, key=pod_sorting_key)
    sorted_nodes = sorted(nodes_to_schedule.values(), key=node_topology_key)

    print(f'Nodes to schedule: {len(nodes_to_schedule)}')

    best_assignment = calculate_pods_assignment(sorted_nodes, sorted_pods)

    if not best_assignment:
      print(
          f'No scheduling for job: {job_name} with gate {gate} has been found.'
          ' Skipping job.'
      )
      continue
    else:
      print(f'Assignment found, scheduling {job_name} with {len(jobs)} pods.')

    for i in range(0, len(sorted_pods)):
      pod = sorted_pods[i]
      node = sorted_nodes[best_assignment[i]]
      schedule_pod_on_node(
          v1, pod['name'], pod['namespace'], node, gate
      )


def run_scheduling_loop():
  """Runs scheduling."""
  parser = argparse.ArgumentParser(
      prog='schedule-workload.py')

  parser.add_argument(
      '-g', '--gate',
      default='gke.io/topology-aware-auto-')    # prefix of the schedule gate
  parser.add_argument(
      '-i', '--interval',
      default=1.0)    # intervals (in seconds) between scheduling
  parser.add_argument(
      '--ignored-namespace',
      nargs='*',
      default=[])     # namespace to search for pods
  args = parser.parse_args()

  try:
    kubernetes.config.load_incluster_config()
  except kubernetes.config.ConfigException:
    kubernetes.config.load_kube_config()
  v1 = kubernetes.client.CoreV1Api()

  def list_pods():
    # filtering of namespace is not cached as namespaces could be
    # created and deleted
    namespaces = v1.list_namespace().items
    filtered_namespace_names = []
    for n in namespaces:
      if n.metadata.name not in args.ignored_namespace:
        filtered_namespace_names.append(n.metadata.name)
    pods = []
    for n in filtered_namespace_names:
      pods += v1.list_namespaced_pod(n).items
    return pods

  try:
    t0 = time.time()
    while True:
      interval = time.time() - t0
      if interval < args.interval:
        time.sleep(args.interval - interval)
      t0 = time.time()

      pods = list_pods()

      gates = find_pod_gates(pods, args.gate)
      print(f"Found {len(pods)} pods and {len(gates)} gates")

      if len(gates) == 0:
        # No pods to be scheduled
        continue

      # sleep for one seconds, assuming that all pods within one group would be
      # all visible by then
      time.sleep(5.0)

      for g in gates:
        print(f"scheduling pods with gate {g}")
        # query the pods again after the sleep, just in case not all gated pods
        # are returned from previous query
        pods = list_pods()
        schedule_pod_with_gate(v1, pods, g)

  except ApiException as e:
    print(f'Exception when listing Kubernetes nodes or pods: {e}')


if __name__ == '__main__':
  run_scheduling_loop()
