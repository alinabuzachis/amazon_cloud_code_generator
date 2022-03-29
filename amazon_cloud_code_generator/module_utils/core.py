# This code is part of Ansible, but is an independent component.
# This particular file snippet, and this file snippet only, is BSD licensed.
# Modules you write using this snippet, which is embedded dynamically by Ansible
# still belong to the author of the module, and may assign their own license
# to the complete work.
#
# (c) 2021 Red Hat Inc.
#
# Redistribution and use in source and binary forms, with or without modification,
# are permitted provided that the following conditions are met:
#
#    * Redistributions of source code must retain the above copyright
#      notice, this list of conditions and the following disclaimer.
#    * Redistributions in binary form must reproduce the above copyright notice,
#      this list of conditions and the following disclaimer in the documentation
#      and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED.
# IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE
# USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

"""
This module_utility adds shared support for AWS Cloud Control API modules.
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type


import json
import traceback
from itertools import count
from typing import Iterable, List, Dict

from ansible_collections.amazon.aws.plugins.module_utils.ec2 import AWSRetry
from ansible_collections.amazon.cloud.plugins.module_utils.utils import (
    JsonPatch,
    make_op,
    op,
    normalize_response,
    scrub_keys,
    to_sync,
    to_async,
    ansible_dict_to_boto3_tag_list,
    snake_dict_to_camel_dict,
)


BOTO3_IMP_ERR = None
try:
    import botocore

    HAS_BOTO3 = True
except ImportError:
    BOTO3_IMP_ERR = traceback.format_exc()
    HAS_BOTO3 = False


class CloudControlResource(object):
    def __init__(self, module):
        """
        Until we can use a connection plugin for auth and client setup, reuse AnsibleAWSModule
        """
        self.module = module
        self.client = module.client(
            "cloudcontrol", retry_decorator=AWSRetry.jittered_backoff()
        )

    @property
    def _waiter_config(self):
        delay = min(10, self.module.params.get("wait_timeout"))
        max_attempts = self.module.params.get("wait_timeout") // delay
        return {"Delay": delay, "MaxAttempts": max_attempts}

    @to_sync
    async def list_resources(self, type_name: str) -> List:
        """
        An exception occurred during task execution. To see the full traceback, use -vvv.
        The error was: botocore.exceptions.OperationNotPageableError: Operation cannot be paginated: list_resources
        Fall to manual pagination
        """
        resource_list: List = []
        results: List = []
        response: Dict = {}

        for i in count():
            # https://docs.aws.amazon.com/cloudcontrolapi/latest/APIReference/API_ListResources.html
            params = {
                # https://docs.aws.amazon.com/cloudcontrolapi/latest/userguide/supported-resources.html
                "TypeName": type_name
            }
            if i == 0 or "NextToken" in response:
                if "NextToken" in response:
                    params["NextToken"] = response["NextToken"]
                try:
                    response = self.client.list_resources(**params)
                except (
                    botocore.exceptions.BotoCoreError,
                    botocore.exceptions.ClientError,
                ) as e:
                    self.module.fail_json_aws(e, msg="Failed to list resources")
                # results.extend(normalize_response(response))
                resource_list.append(response)
            else:
                break

        for each in resource_list:
            resource_descriptions = each.get("ResourceDescriptions", [])

            # for r in resource_descriptions:
            #    identifier = r.get("Identifier")
            #    results.append(self.get_resource(type_name, identifier))

            # Fall to use asyncio to speed up the process
            import asyncio

            futures = [
                self.get_resources_async(type_name, r.get("Identifier"))
                for r in resource_descriptions
            ]
            results = await asyncio.gather(*futures)

        return results

    def list_resource_requests(self, params: Iterable) -> List:
        """
        Returns existing resource operation requests using specific filters.
        """
        results = []
        response = {}

        for i in count():
            # https://docs.aws.amazon.com/cloudcontrolapi/latest/APIReference/API_ListResourceRequests.html
            if i == 0 or "NextToken" in response:
                if "NextToken" in response:
                    params["NextToken"] = response["NextToken"]
                try:
                    response = self.client.list_resource_requests(**params)
                except (
                    botocore.exceptions.BotoCoreError,
                    botocore.exceptions.ClientError,
                ) as e:
                    self.module.fail_json_aws(e, msg="Failed to list resource requests")
                results.extend(response["ResourceRequestStatusSummaries"])
            else:
                break

        return results

    @to_async
    def get_resources_async(self, type_name, identifier):
        return self.get_resource(type_name, identifier)

    def get_resource(self, type_name: str, primary_identifier: str) -> List:
        # This is the "describe" equivalent for CCAPI
        response = {}

        try:
            response = self.client.get_resource(
                TypeName=type_name, Identifier=primary_identifier
            )
        except self.client.exceptions.ResourceNotFoundException:
            return response
        except (
            botocore.exceptions.BotoCoreError,
            botocore.exceptions.ClientError,
        ) as e:
            self.module.fail_json_aws(e, msg="Failed to retrieve resource")

        result = normalize_response(response)
        return result

    def create_resource(self, type_name: str, identifier: str, params: Dict) -> bool:
        changed: bool = False

        try:
            response = self.client.get_resource(
                TypeName=type_name, Identifier=identifier
            )
        except self.client.exceptions.ResourceNotFoundException:
            if not self.module.check_mode:
                try:
                    response = self.client.create_resource(
                        TypeName=type_name, DesiredState=params
                    )
                    self.client.get_waiter("resource_request_success").wait(
                        RequestToken=response["ProgressEvent"]["RequestToken"],
                        WaiterConfig=self._waiter_config,
                    )
                except botocore.exceptions.WaiterError as e:
                    self.module.fail_json_aws(
                        e,
                        msg="An error occurred waiting for the resource request to become successful",
                    )
            changed: bool = True
        except (
            botocore.exceptions.BotoCoreError,
            botocore.exceptions.ClientError,
        ) as e:
            self.module.fail_json_aws(e, msg="Failed to created resource")

        return changed

    def check_in_progress_requests(self, type_name: str, identifier: str):
        in_progress_requests = []
        params = {
            "ResourceRequestStatusFilter": {
                "Operations": ["CREATE", "DELETE", "UPDATE"],
                "OperationStatuses": ["IN_PROGRESS"],
            }
        }

        response = self.list_resource_requests(params)

        if response:
            in_progress_requests = list(
                filter(
                    lambda d: d["TypeName"] == type_name
                    and d["Identifier"] == identifier,
                    response,
                )
            )

            if in_progress_requests:
                self.module.warn(
                    f"There is one or more IN PROGRESS operations on {identifier}. Wait until there are no more IN PROGRESS operations before proceding."
                )
                for e in in_progress_requests:
                    self.client.get_waiter("resource_request_success").wait(
                        RequestToken=e["RequestToken"]
                    )

    def delete_resource(self, type_name: str, identifier: str) -> bool:
        changed: bool = False

        try:
            response = self.client.get_resource(
                TypeName=type_name, Identifier=identifier
            )
        except self.client.exceptions.ResourceNotFoundException:
            return changed

        try:
            if not self.module.check_mode:
                self.check_in_progress_requests(type_name, identifier)

                response = self.client.delete_resource(
                    TypeName=type_name, Identifier=identifier
                )

                if self.module.params.get("wait"):
                    try:
                        self.client.get_waiter("resource_request_success").wait(
                            RequestToken=response["ProgressEvent"]["RequestToken"],
                            WaiterConfig=self._waiter_config,
                        )
                    except botocore.exceptions.WaiterError as e:
                        self.module.fail_json_aws(
                            e,
                            msg="An error occurred waiting for the resource request to become successful.",
                        )
            changed = True
        except (
            botocore.exceptions.BotoCoreError,
            botocore.exceptions.ClientError,
        ) as e:
            self.module.fail_json_aws(e, msg="Failed to delete resource")

        return changed

    def update_resource(
        self,
        type_name: str,
        identifier: str,
        params_to_set: Dict,
        create_only_params: List,
    ) -> bool:
        changed: bool = False

        try:
            response = self.client.get_resource(
                TypeName=type_name, Identifier=identifier
            )
        except self.client.exceptions.ResourceNotFoundException:
            return changed
        except (
            botocore.exceptions.BotoCoreError,
            botocore.exceptions.ClientError,
        ) as e:
            self.module.fail_json_aws(e, msg="Failed to retrieve resource")

        properties = response.get("ResourceDescription", {}).get("Properties", {})
        properties = json.loads(properties)

        # Ignore createOnlyProperties that can be set only during resource creation
        params = scrub_keys(params_to_set, create_only_params)

        patch = JsonPatch()
        for k, v in params.items():
            strategy = "merge"
            if v == properties.get(k):
                continue
            if k not in properties:
                patch.append(op("add", k, v))
            else:
                if self.module.params.get("purge_{0}".format(k.lower())):
                    strategy = "replace"
                patch.append(make_op(k, properties[k], v, strategy))

        if patch:
            try:
                if not self.module.check_mode:
                    self.check_in_progress_requests(type_name, identifier)

                    response = self.client.update_resource(
                        TypeName=type_name,
                        Identifier=identifier,
                        PatchDocument=str(patch),
                    )

                    if self.module.params.get("wait"):
                        try:
                            self.client.get_waiter("resource_request_success").wait(
                                RequestToken=response["ProgressEvent"]["RequestToken"],
                                WaiterConfig=self._waiter_config,
                            )
                        except botocore.exceptions.WaiterError as e:
                            self.module.fail_json_aws(
                                e,
                                msg="An error occurred waiting for the resource request to become successful.",
                            )
                changed = True
            except (
                botocore.exceptions.BotoCoreError,
                botocore.exceptions.ClientError,
            ) as e:
                self.module.fail_json_aws(e, msg="Failed to update resource")

        return changed
