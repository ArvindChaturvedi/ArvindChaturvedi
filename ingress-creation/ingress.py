import boto3
import kubernetes
from kubernetes.client import (
    V1ObjectMeta,
    V1Ingress,
    V1IngressSpec,
    V1IngressRule,
    V1HTTPIngressPath,
    V1HTTPIngressRuleValue,
    V1IngressBackend,
    V1IngressServiceBackend,
    V1ServiceBackendPort
)
from kubernetes.client.rest import ApiException
from botocore.exceptions import ClientError
import re

# AWS Clients initialized with IRSA permissions
ec2_client = boto3.client('ec2')
elbv2_client = boto3.client('elbv2')

# Kubernetes client initialized using the in-cluster configuration
kubernetes.config.load_incluster_config()
v1 = kubernetes.client.NetworkingV1Api()

NAMESPACE = "system-application"
TARGET_NAMESPACE = "kube-system"

def get_load_balancers_by_tag(key, value_prefix):
    """Retrieve ALBs with tags matching the given key and value prefix."""
    try:
        response = elbv2_client.describe_load_balancers()
        load_balancers = response['LoadBalancers']

        # Filter ALBs based on tag value prefix
        filtered_albs = []
        for alb in load_balancers:
            alb_arn = alb['LoadBalancerArn']
            tags = elbv2_client.describe_tags(ResourceArns=[alb_arn])['TagDescriptions'][0]['Tags']
            alb_tags = {tag['Key']: tag['Value'] for tag in tags}
            
            # Ensure the value of 'ingress.k8s.aws/stack' starts with the provided prefix
            if alb_tags.get(key, '').startswith(value_prefix):
                alb['Tags'] = alb_tags  # Attach tags to ALB for later use
                filtered_albs.append(alb)
        return filtered_albs
    except ClientError as e:
        print(f"Error retrieving ALBs by tag {key} with value starting with {value_prefix}: {e}")
        return []

def get_security_groups_from_tag(tag_key, tag_value):
    """Retrieve Security Groups associated with ALBs having specific tag values."""
    try:
        security_groups = []
        albs = get_load_balancers_by_tag(tag_key, tag_value)
        for alb in albs:
            sg_ids = get_security_groups_from_alb(alb)
            if sg_ids:
                security_groups.extend(sg_ids)
        return list(set(security_groups))  # Remove duplicates
    except ClientError as e:
        print(f"Error retrieving security groups for tag {tag_key} with value {tag_value}: {e}")
        return []

def get_security_groups_from_alb(alb):
    """Retrieve the security groups associated with an ALB."""
    try:
        return alb['SecurityGroups']
    except KeyError:
        print(f"Error: No SecurityGroups associated with ALB {alb['LoadBalancerName']}")
        return []

def create_ingress_object(alb, security_groups, is_external):
    """Create an Ingress object based on the ALB type."""
    alb_name = alb['LoadBalancerName']
    ingress_name = f"system-{alb_name}-ingress"
    
    # Get the value of 'ingress.k8s.aws/stack' for group.name
    group_name = alb['Tags'].get('ingress.k8s.aws/stack', 'default')

    ingress_annotations = {
        "alb.ingress.kubernetes.io/load-balancer-name": alb_name,
        "alb.ingress.kubernetes.io/security-groups": ','.join(security_groups),
        "alb.ingress.kubernetes.io/scheme": "internet" if is_external else "internal",
        "alb.ingress.kubernetes.io/listen-ports": '[{"HTTPS":443}]',
        "alb.ingress.kubernetes.io/target-type": "ip",
        "alb.ingress.kubernetes.io/group.name": group_name,
        "alb.ingress.kubernetes.io/group.order": "-1000"
    }

    # Customizing annotations based on ALB type
    if is_external:
        ingress_annotations["alb.ingress.kubernetes.io/actions.healthcheck-v2"] = (
            '{"type":"fixed-response","fixedResponseConfig":{"contentType":"text/plain","statusCode":"200","messageBody":"HEALTHY"}}'
        )
        ingress_path = "/healthcheck"
    else:
        ingress_annotations["alb.ingress.kubernetes.io/actions.listener-protection-v2"] = (
            '{"type":"fixed-response","fixedResponseConfig":{"contentType":"text/plain","statusCode":"200","messageBody":"Secure Listener Protection"}}'
        )
        ingress_path = "/sys-internal"

    # Define the Ingress spec
    ingress_spec = V1IngressSpec(
        ingress_class_name="alb",
        rules=[
            V1IngressRule(
                http=V1HTTPIngressRuleValue(
                    paths=[
                        V1HTTPIngressPath(
                            path=ingress_path,
                            path_type="Exact",
                            backend=V1IngressBackend(
                                service=V1IngressServiceBackend(
                                    name="healthcheck-v2",
                                    port=V1ServiceBackendPort(name="use-annotation")
                                )
                            )
                        )
                    ]
                )
            )
        ]
    )

    # Create the Ingress object
    ingress = V1Ingress(
        metadata=V1ObjectMeta(
            name=ingress_name,
            namespace=TARGET_NAMESPACE,  # Ensure it's created in kube-system namespace
            annotations=ingress_annotations
        ),
        spec=ingress_spec
    )
    return ingress

def apply_ingress(ingress):
    """Apply the Ingress object to the Kubernetes cluster."""
    try:
        existing_ingress = v1.read_namespaced_ingress(
            name=ingress.metadata.name, namespace=TARGET_NAMESPACE
        )
        print(f"Ingress {ingress.metadata.name} already exists.")
    except ApiException as e:
        if e.status == 404:
            try:
                v1.create_namespaced_ingress(namespace=TARGET_NAMESPACE, body=ingress)
                print(f"Ingress {ingress.metadata.name} created successfully in {TARGET_NAMESPACE}.")
            except ApiException as create_error:
                print(f"Error creating ingress {ingress.metadata.name}: {create_error}")
        else:
            print(f"Error checking ingress {ingress.metadata.name}: {e}")

def main():
    # Retrieve security groups associated with ALBs tagged 'shared-external'
    external_sgs = get_security_groups_from_tag('ingress.k8s.aws/stack', 'shared-external')

    # Get External ALBs with the tag key 'ingress.k8s.aws/stack' and value starting with 'shared-external-'
    external_albs = get_load_balancers_by_tag('ingress.k8s.aws/stack', 'shared-external-')
    
    # Apply Ingresses for External ALBs
    for alb in external_albs:
        ingress = create_ingress_object(alb, external_sgs, is_external=True)
        apply_ingress(ingress)

    # Retrieve security groups associated with ALBs tagged 'shared-internal'
    internal_sgs = get_security_groups_from_tag('ingress.k8s.aws/stack', 'shared-internal')

    # Get Internal ALBs with the tag key 'ingress.k8s.aws/stack' and value starting with 'shared-internal-'
    internal_albs = get_load_balancers_by_tag('ingress.k8s.aws/stack', 'shared-internal-')
    
    # Apply Ingresses for Internal ALBs
    for alb in internal_albs:
        ingress = create_ingress_object(alb, internal_sgs, is_external=False)
        apply_ingress(ingress)

if __name__ == "__main__":
    main()
