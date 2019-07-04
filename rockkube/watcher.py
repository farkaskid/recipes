from kubernetes import client, config, watch
import threading
import rockset
import os
import argparse

# Before watching events, connect to the rockset client
ROCKSET_API_KEY = os.environ['ROCKSET_API_KEY'] # replace with your API key here
COLLECTION_NAME = 'kubernetes_events'
rs = rockset.Client(api_key=ROCKSET_API_KEY)

# Fetch collection called kubernetes_events. If it does not exist, create it
try:
    collection = rs.Collection.retrieve(COLLECTION_NAME)
except rockset.exception.InputError as e:
    collection = rs.Collection.create(COLLECTION_NAME)

# Configs can be set in Configuration class directly or using helper utility
# Here, we are loading it from the default location
config.load_kube_config()

# create kubernetes API instance
v1 = client.CoreV1Api()
v1ext = client.ExtensionsV1beta1Api()


def generate_event_info(event, watch):
    # The events are structured slightly differently between 
    # batch ingest and watcher streams, so we 
    # have two cases for generating the event_info document
    if watch:
        event_info = event
    else:
        event_info = event.to_dict()
    return event_info


# Because we get a large number of events at the start of watching 
# We batch these into several groups with max size limit_size
# This function returns the resource version, which is the number before which
# our watcher does not return events. This prevents us from getting 
# duplicates when we stream the events for all namespaces
def batch_upload_events():
    resource_version = 0
    continue_tok = ''
    while True:
        limit_size = 500
        events = v1.list_event_for_all_namespaces(limit=limit_size, _continue = continue_tok)
        continue_tok = events.metadata._continue
        resource_version = events.metadata.resource_version
        event_docs = []
        for event in events.items:
            event_docs.append(generate_event_info(event, False))
        collection.add_docs(event_docs)
        if not continue_tok:
            break
    return resource_version

# Create a watcher around the stream, and then
# for each event, process the data from the event
# and send to rockset as part of the kubernetes_events collection
def watch_events(max_failures):
    w = watch.Watch()
    resource_version = batch_upload_events()
    doc_buffer = []
    consecutive_failures = 0
    # Here, resource version allows us to not fetch a large batch of events
    for event in w.stream(v1.list_event_for_all_namespaces, resource_version=resource_version):
        event_info = generate_event_info(event, True)
        doc_buffer.append(event_info)
        # try to add all pending docs to the rockset collection
        # if we are successful, we can flush the buffer and reset fail count
        try:
            # TODO: save resource version here so we don't duplicate updates
            rs_info = collection.add_docs(doc_buffer)
            doc_buffer = []
            consecutive_failures = 0
        # For some reason, the rockset client failed, so we 
        # keep the doc in the buffer and try again on the next event
        except Exception as e:
            consecutive_failures += 1
            print("Failed uploading to Rockset {} consecutive times".format(consecutive_failures, max_failures))
            # If we fail MAX_FAILURES times, assume there is some kind of 
            # external issue and stop the watcher.
            if len(doc_buffer) > max_failures:
                print("ERROR: Failed to upload to rockset {} times in a row. Terminating watcher.".format(max_failures))
                break


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("max_failures", nargs='?', default=25)
    args = parser.parse_args()
    watch_events(args.max_failures)
