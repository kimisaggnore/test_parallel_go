from azure.mgmt.resource import ResourceManagementClient
from azure.identity import DefaultAzureCredential

class Taggable:
    """Base class for tagging Azure resources."""

    def __init__(self, subscription_id: str):
        self.subscription_id = subscription_id
        self.resource_client = ResourceManagementClient(DefaultAzureCredential(), subscription_id)

    def _perform_tag_update(self, scope: str, tags: dict) -> None:
        """Perform the tagging operation (to be implemented)."""
        # Here you would implement the logic to update tags using the Azure SDK
        print(f"Updating tags for {scope} with {tags}")

class SubscriptionTagger(Taggable):
    """Class for tagging Azure subscriptions."""

    def update_tags(self, tag_config: dict) -> None:
        """Update tags for the subscription."""
        tags = tag_config.get("tags", {})
        scope = f"/subscriptions/{self.subscription_id}"
        self._perform_tag_update(scope, tags)

class ResourceGroupTagger(Taggable):
    """Class for tagging Azure resource groups."""

    def __init__(self, subscription_id: str, resource_group_name: str):
        super().__init__(subscription_id)
        self.resource_group_name = resource_group_name

    def update_tags(self, tag_config: dict) -> None:
        """Update tags for the resource group."""
        tags = tag_config.get("tags", {})
        scope = f"/subscriptions/{self.subscription_id}/resourceGroups/{self.resource_group_name}"
        self._perform_tag_update(scope, tags)

class ResourceTagger(ResourceGroupTagger):
    """Class for tagging Azure resources."""
    
    def __init__(self, subscription_id: str, resource_group_name: str, resource_name: str):
        super().__init__(subscription_id, resource_group_name)
        self.resource_name = resource_name
        self.resource_provider, self.resource_type = self._get_resource_details()

    def _get_resource_details(self) -> tuple:
        """Retrieve resource provider and type based on the resource name."""
        resources = self.resource_client.resources.list_by_resource_group(self.resource_group_name)
        for resource in resources:
            if resource.name == self.resource_name:
                return resource.type.split("/")[0], resource.type.split("/")[1]
        raise ValueError(f"Resource {self.resource_name} not found in resource group {self.resource_group_name}.")

    def update_tags(self, tag_config: dict) -> None:
        """Update tags for the specific resource."""
        tags = tag_config.get("tags", {})
        scope = f"/subscriptions/{self.subscription_id}/resourceGroups/{self.resource_group_name}/providers/{self.resource_provider}/{self.resource_type}/{self.resource_name}"
        self._perform_tag_update(scope, tags)

# Example Usage
if __name__ == "__main__":
    subscription_id = "your_subscription_id"
    resource_group_name = "your_resource_group_name"
    resource_name = "your_resource_name"

    # Tagging a subscription
    subscription_tagger = SubscriptionTagger(subscription_id)
    subscription_tagger.update_tags({"tags": {"Environment": "Production", "Owner": "Team A"}})

    # Tagging a resource group
    resource_group_tagger = ResourceGroupTagger(subscription_id, resource_group_name)
    resource_group_tagger.update_tags({"tags": {"Environment": "Test"}})

    # Tagging an individual resource
    resource_tagger = ResourceTagger(subscription_id, resource_group_name, resource_name)
    resource_tagger.update_tags({"tags": {"Role": "WebServer"}})
