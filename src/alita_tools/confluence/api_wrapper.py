import json
import logging
import re
import traceback
from json import JSONDecodeError
from typing import List, Optional, Any, Dict, Callable, Generator

import requests
from langchain_community.document_loaders.confluence import ContentFormat
from langchain_core.documents import Document
from langchain_core.tools import ToolException
from markdownify import markdownify
from pydantic import create_model, Field, model_validator, SecretStr
from pydantic.fields import PrivateAttr
from tenacity import (
    before_sleep_log,
    retry,
    stop_after_attempt,
    wait_exponential,
)

from ..elitea_base import BaseToolApiWrapper
from ..utils import is_cookie_token, parse_cookie_string

logger = logging.getLogger(__name__)

createPage = create_model(
    "createPage",
    space=(Optional[str], Field(description="Confluence space that is used for page's creation", default=None)),
    title=(str, Field(description="Title of the page")),
    body=(str, Field(description="Body of the page")),
    status=(Optional[str], Field(description="Page publishing option: 'current' for publish page, 'draft' to create draft.", default='current')),
    parent_id=(Optional[str], Field(description="Page parent id (optional)", default=None)),
    representation=(Optional[str], Field(description="Content representation format: storage for html, wiki for markdown", default='storage')),
    label=(Optional[str], Field(description="Page label (optional)", default=None)),
)

createPages = create_model(
    "createPages",
    space=(Optional[str], Field(description="Confluence space that is used for pages creation", default=None)),
    pages_info=(str, Field(description="""JSON string containing information about page name and its content per syntax: [{"page1_name": "page1_content"}, {"page2_name": "page2_content"}]""")),
    parent_id=(Optional[str], Field(description="Page parent id (optional)", default=None)),
    status=(Optional[str], Field(description="Page publishing option: 'current' for publish page, 'draft' to create draft.", default='current')),
)

deletePage = create_model(
    "deletePage",
    page_id=(Optional[str], Field(description="Page id", default=None)),
    page_title=(Optional[str], Field(description="Page title", default=None)),
)

updatePageById = create_model(
    "updatePageById",
    page_id=(str, Field(description="Page id")),
    representation=(Optional[str], Field(description="Content representation format: storage for html, wiki for markdown", default='storage')),
    new_title=(Optional[str], Field(description="New page title", default=None)),
    new_body=(Optional[str], Field(description="New page content", default=None)),
    new_labels=(Optional[list], Field(description="Page labels", default=None)),
)

updatePageByTitle = create_model(
    "updatePageByTitle",
    page_title=(str, Field(description="Page title")),
    representation=(Optional[str], Field(description="Content representation format: storage for html, wiki for markdown", default='storage')),
    new_title=(Optional[str], Field(description="New page title", default=None)),
    new_body=(Optional[str], Field(description="New page content", default=None)),
    new_labels=(Optional[list], Field(description="Page labels", default=None)),
)

updatePages = create_model(
    "updatePages",
    page_ids=(Optional[list], Field(description="List of ids of pages to be updated", default=None)),
    new_contents=(Optional[list], Field(description="List of new contents for each page. If content the same for all the pages then it should be a list with a single entry", default=None)),
    new_labels=(Optional[list], Field(description="Page labels", default=None)),
)

updateLabels = create_model(
    "updateLabels",
    page_ids=(Optional[list], Field(description="List of ids of pages to be updated", default=None)),
    new_labels=(Optional[list], Field(description="Page labels", default=None)),
)

getPageTree = create_model(
    "getPageTree",
    page_id=(str, Field(description="Page id")),
)

pageExists = create_model(
    "pageExists",
    title=(str, Field(description="Title of the page")),
)

getPagesWithLabel = create_model(
    "getPagesWithLabel",
    label=(str, Field(description="Label of the pages")),
)

searchPages = create_model(
    "searchPages",
    query=(str, Field(description="Query text to search pages")),
    skip_images=(Optional[bool], Field(description="Whether we need to skip existing images or not", default=False))
)

siteSearch = create_model(
    "siteSearch",
    query=(str, Field(description="Query text to execute site search in Confluence")),
)

pageId = create_model(
    "pageId",
    page_id=(str, Field(description="Id of page to be read")),
    skip_images=(Optional[bool], Field(description="Whether we need to skip existing images or not", default=False)),
)

сonfluenceInput = create_model(
     "сonfluenceInput",
     method=(str, Field(description="The HTTP method to use for the request (GET, POST, PUT, DELETE, etc.). Required parameter.")),
     relative_url=(str, Field(description="Required parameter: The relative URI for Confluence API. URI must start with a forward slash and '/rest/...'. Do not include query parameters in the URL, they must be provided separately in 'params'.")),
     params=(Optional[str], Field(default="", description="Optional JSON of parameters to be sent in request body or query params. MUST be string with valid JSON. For search/read operations, you MUST always get minimum fields and set max results, until users ask explicitly for more fields. For search/read operations you must generate CQL query string and pass it as params."))
 )

loaderParams = create_model(
    "LoaderParams",
    content_format=(str, Field(description="The format of the content to be retrieved.")),
    page_ids=(Optional[List[str]], Field(description="List of page IDs to retrieve.", default=None)),
    label=(Optional[str], Field(description="Label to filter pages.", default=None)),
    cql=(Optional[str], Field(description="CQL query to filter pages.", default=None)),
    include_restricted_content=(Optional[bool], Field(description="Include restricted content.", default=False)),
    include_archived_content=(Optional[bool], Field(description="Include archived content.", default=False)),
    include_attachments=(Optional[bool], Field(description="Include attachments.", default=False)),
    include_comments=(Optional[bool], Field(description="Include comments.", default=False)),
    include_labels=(Optional[bool], Field(description="Include labels.", default=False)),
    limit=(Optional[int], Field(description="Limit the number of results.", default=10)),
    max_pages=(Optional[int], Field(description="Maximum number of pages to retrieve.", default=1000)),
    ocr_languages=(Optional[str], Field(description="OCR languages for processing attachments.", default=None)),
    keep_markdown_format=(Optional[bool], Field(description="Keep the markdown format.", default=True)),
    keep_newlines=(Optional[bool], Field(description="Keep newlines in the content.", default=True)),
    bins_with_llm=(Optional[bool], Field(description="Use LLM for processing binary files.", default=False)),
)


def parse_payload_params(params: Optional[str]) -> Dict[str, Any]:
    if params:
        try:
            return json.loads(params)
        except JSONDecodeError:
            stacktrace = traceback.format_exc()
            return ToolException(f"Confluence tool exception. Passed params are not valid JSON. {stacktrace}")
    return {}

class ConfluenceAPIWrapper(BaseToolApiWrapper):
    _client: Any = PrivateAttr()
    base_url: str
    api_key: Optional[SecretStr] = None,
    username: Optional[str] = None
    token: Optional[SecretStr] = None
    cloud: Optional[bool] = True
    limit: Optional[int] = 5
    labels: Optional[List[str]] = []
    space: Optional[str] = None
    max_pages: Optional[int] = 10
    content_format: Optional[ContentFormat] = ContentFormat.VIEW
    include_attachments: Optional[bool] = False
    include_comments: Optional[bool] = False
    include_restricted_content: Optional[bool] = False
    number_of_retries: Optional[int] = 3
    min_retry_seconds: Optional[int] = 2
    max_retry_seconds: Optional[int] = 10
    keep_markdown_format: Optional[bool] = True
    ocr_languages: Optional[str] = None
    keep_newlines: Optional[bool] = True
    alita: Any = None
    llm: Any = None

    @model_validator(mode='before')
    @classmethod
    def validate_toolkit(cls, values):
        try:
            from atlassian import Confluence  # noqa: F401
        except ImportError:
            raise ImportError(
                "`atlassian` package not found, please run "
                "`pip install atlassian-python-api`"
            )

        url = values['base_url']
        api_key = values.get('api_key')
        username = values.get('username')
        token = values.get('token')
        cloud = values.get('cloud')
        if token and is_cookie_token(token):
            session = requests.Session()
            session.cookies.update(parse_cookie_string(token))
            cls._client = Confluence(url=url, session=session, cloud=cloud)
        elif token:
            cls._client = Confluence(url=url, token=token, cloud=cloud)
        else:
            cls._client = Confluence(url=url,username=username, password=api_key, cloud=cloud)
        return values

    def __unquote_confluence_space(self) -> str | None:
        if self.space:
            # Remove single quotes or backticks if present
            if (self.space.startswith("'") and self.space.endswith("'")) or (
                    self.space.startswith("`") and self.space.endswith("`")):
                return self.space[1:-1]
            else:
                return self.space
        else:
            return None

    def __sanitize_confluence_space(self) -> str:
        """Ensure the space parameter is enclosed in double quotes."""
        unquoted_space = self.__unquote_confluence_space()
        if unquoted_space:
            # Add double quotes if not already present
            if not (unquoted_space.startswith('"') and unquoted_space.endswith('"')):
                unquoted_space = f'"{unquoted_space}"'
        return unquoted_space

    def create_page(self, title: str, body: str, status: str = 'current', space: str = None, parent_id: str = None, representation: str = 'storage', label: str = None):
        """ Creates a page in the Confluence space. Represents content in html (storage) or wiki (wiki) formats
            Page could be either published status='current' or make a draft with status='draft'
        """
        if self._client.get_page_by_title(space=self.space, title=title) is not None:
            return f"Page with title {title} already exists, please use other title."

        # normal user flow: put pages in the Space Home, not in the root of the Space
        user_space = space if space else self.space
        logger.info(f"Page will be created within the space {user_space}")
        parent_id_filled = parent_id if parent_id else self._client.get_space(user_space)['homepage']['id']

        created_page = self.temp_create_page(space=user_space, title=title, body=body, status=status, parent_id=parent_id_filled, representation=representation)

        page_details = {
            'title': created_page['title'],
            'id': created_page['id'],
            'space key': created_page['space']['key'],
            'author': created_page['version']['by']['displayName'],
            'link': created_page['_links']['base'] + (created_page['_links']['edit'] if status == 'draft' else created_page['_links']['webui'])
        }

        logger.info(f"Page created: {page_details['link']}")

        if label:
            self._client.set_page_label(page_id=created_page['id'], label=label)
            logger.info(f"Label '{label}' added to the page '{title}'.")
            page_details['label'] = label

        self._add_default_labels(page_id=created_page['id'])

        return f"The page '{title}' was created under the parent page '{parent_id_filled}': '{page_details['link']}'. \nDetails: {str(page_details)}"

    def create_pages(self, pages_info: str, status: str = 'current', space: str = None, parent_id: str = None):
        """ Creates a batch of pages in the Confluence space."""
        created_pages = []
        user_space = space if space else self.space
        logger.info(f"Pages will be created within the space {user_space}")
        # duplicate action to avoid extra api calls in downstream function
        parent_id_filled = parent_id if parent_id else self._client.get_space(user_space)['homepage']['id']
        for page_item in json.loads(pages_info):
            for title, body in page_item.items():
                created_page = self.create_page(title=title, body=body, status=status, parent_id=parent_id_filled, space=user_space)
                created_pages.append(created_page)
        return str(created_pages)

    # delete after https://github.com/atlassian-api/atlassian-python-api/pull/1452 will be merged
    def temp_create_page(self, space, title, body, parent_id=None, type="page", representation="storage", editor=None, full_width=False, status='current'):
        logger.info('Creating %s "%s" -> "%s"', type, space, title)
        url = "rest/api/content/"
        data = {
            "type": type,
            "title": title,
            "status": status,
            "space": {"key": space},
            "body": self._client._create_body(body, representation),
            "metadata": {"properties": {}},
        }
        if parent_id:
            data["ancestors"] = [{"type": type, "id": parent_id}]
        if editor is not None and editor in ["v1", "v2"]:
            data["metadata"]["properties"]["editor"] = {"value": editor}
        if full_width is True:
            data["metadata"]["properties"]["content-appearance-draft"] = {"value": "full-width"}
            data["metadata"]["properties"]["content-appearance-published"] = {"value": "full-width"}
        else:
            data["metadata"]["properties"]["content-appearance-draft"] = {"value": "fixed-width"}
            data["metadata"]["properties"]["content-appearance-published"] = {"value": "fixed-width"}

        return self._client.post(url, data=data)

    def delete_page(self, page_id: str = None, page_title: str = None):
        """ Deletes a page by its defined page_id or page_title """
        if not page_id and not page_title:
            raise ValueError("Either page_id or page_title is required to delete the page")
        resolved_page_id = page_id if page_id else (self._client.get_page_by_title(space=self.space, title=page_title) or {}).get('id')
        if resolved_page_id:
            self._client.remove_page(resolved_page_id)
            message = f"Page with ID '{resolved_page_id}' has been successfully deleted."
        else:
            message = f"Page instance could not be resolved with id '{page_id}' and/or title '{page_title}'"
        return message

    def update_page_by_id(self, page_id: str, representation: str = 'storage', new_title: str = None, new_body: str = None, new_labels: list = None):
        """ Updates an existing Confluence page (using id or title) by replacing its content, title, labels """
        current_page = self._client.get_page_by_id(page_id, expand='version,body.view')
        if not current_page:
            return f"Page with ID {page_id} not found."

        if new_title and current_page['title'] != new_title and self._client.get_page_by_title(space=self.space, title=new_title):
            return f"Page with title {new_title} already exists."

        current_version = current_page['version']['number']
        title_to_use = new_title if new_title else current_page['title']
        body_to_use = new_body if new_body else current_page['body']['view']['value']
        representation_to_use = representation if representation else current_page['body']['view']['representation']

        updated_page = self._client.update_page(page_id=page_id, title=title_to_use, body=body_to_use, representation=representation_to_use)
        webui_link = updated_page['_links']['base'] + updated_page['_links']['webui']
        logger.info(f"Page updated: {webui_link}")

        next_version = updated_page['version']['number']
        diff_link = f"{updated_page['_links']['base']}/pages/diffpagesbyversion.action?pageId={page_id}&selectedPageVersions={current_version}&selectedPageVersions={next_version}"
        logger.info(f"Link to diff: {diff_link}")

        update_details = {
            'title': updated_page['title'],
            'id': updated_page['id'],
            'space key': updated_page['space']['key'],
            'author': updated_page['version']['by']['displayName'],
            'link': updated_page['_links']['base'] + updated_page['_links']['webui'],
            'version': next_version,
            'diff': diff_link
        }

        if new_labels is not None:
            current_labels = self._client.get_page_labels(page_id)
            for label in current_labels['results']:
                self._client.remove_page_label(page_id, label['name'])
            for label in new_labels:
                self._client.set_page_label(page_id, label)
            logger.info(f"Labels updated for the page '{title_to_use}'.")
            update_details['labels'] = new_labels

        self._add_default_labels(page_id=page_id)

        return f"The page '{page_id}' was updated successfully: '{webui_link}'. \nDetails: {str(update_details)}"

    def update_page_by_title(self, page_title: str, representation: str = 'storage', new_title: str = None, new_body: str = None, new_labels: list = None):
        """ Updates an existing Confluence page (using id or title) by replacing its content, title, labels """
        current_page = self._client.get_page_by_title(space=self.space, title=page_title)
        if not current_page:
            return f"Page with title {page_title} not found."

        return self.update_page_by_id(page_id=current_page['id'], representation=representation, new_title=new_title, new_body=new_body, new_labels=new_labels)

    def update_pages(self, page_ids: list = None, new_contents: list = None, new_labels: list = None):
        """ Update a batch of pages in the Confluence space. """
        statuses = []
        if len(page_ids) != len(new_contents) and len(new_contents) != 1:
            raise ValueError("New content should be provided for all the pages or it should contain only 1 new body for bulk update")
        if page_ids:
            for index, page_id in enumerate(page_ids):
                status = self.update_page_by_id(page_id=page_id, new_body=new_contents[index if len(new_contents) != 1 else 0], new_labels=new_labels)
                statuses.append(status)
            return str(statuses)
        else:
            return "Either list of page_ids or parent_id (to update descendants) should be provided."

    def _add_default_labels(self, page_id: str):
        """ Add default labels to the pages that has been created or modified by agent."""

        if self.labels:
            logger.info(f'Add pre-defined labels to the issue: {self.labels}')
            for label in self.labels:
                self._client.set_page_label(page_id, label)

    def update_labels(self, page_ids: list = None, new_labels: list = None):
        """ Update a batch of pages in the Confluence space. """
        statuses = []
        if page_ids:
            for index, page_id in enumerate(page_ids):
                status = self.update_page_by_id(page_id=page_id, new_labels=new_labels)
                statuses.append(status)
            return str(statuses)
        else:
            return "Either list of page_ids should be provided."

    def get_page_tree(self, page_id: str):
        """ Gets page tree for the Confluence space """
        descendant_pages = self.get_all_descendants(page_id)  # Pass None as the parent for the root
        for page in descendant_pages:
            logger.info(f"Page ID: {page['id']}, Title: {page['title']}, Parent ID: {page['parent_id']}")
        descendants = {page['id']: (page['title'], page['parent_id']) for page in descendant_pages}
        return f"The list of pages under the '{page_id}' was extracted: {descendants}"

    def get_all_descendants(self, page_id: str):
        """ Recursively gets all descendant pages of a given page. """
        descendants = []
        limit = 100
        start = 0

        while True:
            children = self._client.get_page_child_by_type(page_id, type='page', start=start, limit=limit)
            if not children:
                break
            for child in children:
                child_info = {'id': child['id'], 'title': child['title'], 'parent_id': page_id}
                descendants.append(child_info)
                descendants.extend(self.get_all_descendants(child['id']))
            start += limit

        return descendants

    def page_exists(self, title: str):
        """ Checks if a page exists in the Confluence space."""
        status = self._client.page_exists(space=self.space, title=title)
        return status

    def get_pages_with_label(self, label: str):
        """ Gets pages with specific label in the Confluence space."""
        return str(self._get_labeled_page(label))

    def list_pages_with_label(self, label: str):
        """ Lists the pages with specific label in the Confluence space."""
        return [{'id': page['page_id'], 'title': page['page_title']} for page in self._get_labeled_page(label)]

    def _get_labeled_page(self, label: str):
        """Gets pages with specific label in the Confluence space."""

        start = 0
        pages_info = []
        for _ in range((self.max_pages + self.limit - 1) // self.limit):
            pages = self._client.get_all_pages_by_label(label, start=start, limit=self.limit) #, expand="body.view.value"
            if not pages:
                break

            pages_info += [{
                'page_id': page.metadata['id'],
                'page_title': page.metadata['title'],
                'page_url': page.metadata['source'],
                'content': page.page_content
            } for page in self.get_pages_by_id([page["id"] for page in pages])]
            start += self.limit
        return pages_info

    def is_public_page(self, page: dict) -> bool:
        """Check if a page is publicly accessible."""
        restrictions = self._client.get_all_restrictions_for_content(page["id"])

        return (
                page["status"] == "current"
                and not restrictions["read"]["restrictions"]["user"]["results"]
                and not restrictions["read"]["restrictions"]["group"]["results"]
        )

    def get_pages_by_id(self, page_ids: List[str], skip_images: bool = False):
        """ Gets pages by id in the Confluence space."""
        for page_id in page_ids:
            get_page = retry(
                reraise=True,
                stop=stop_after_attempt(
                    self.number_of_retries  # type: ignore[arg-type]
                ),
                wait=wait_exponential(
                    multiplier=1,  # type: ignore[arg-type]
                    min=self.min_retry_seconds,  # type: ignore[arg-type]
                    max=self.max_retry_seconds,  # type: ignore[arg-type]
                ),
                before_sleep=before_sleep_log(logger, logging.WARNING),
            )(self._client.get_page_by_id)
            page = get_page(
                page_id=page_id, expand=f"{self.content_format.value},version"
            )
            if not self.include_restricted_content and not self.is_public_page(page):
                continue
            yield self.process_page(page, skip_images)

    def read_page_by_id(self, page_id: str, skip_images: bool = False):
        """Reads a page by its id in the Confluence space."""

        result = list(self.get_pages_by_id([page_id], skip_images))
        if not result:
            "Page not found"
        return result[0].page_content
        # return self._strip_base64_images(result[0].page_content) if skip_images else result[0].page_content

    def _strip_base64_images(self, content):
        base64_md_pattern = r'data:image/(png|jpeg|gif);base64,[a-zA-Z0-9+/=]+'
        return re.sub(base64_md_pattern, "[Image Removed]", content)

    def _process_search(self, cql, skip_images:bool = False):
        start = 0
        pages_info = []
        for _ in range((self.max_pages + self.limit - 1) // self.limit):
            pages = self._client.cql(cql, start=start, limit=self.limit).get("results", [])
            if not pages:
                break
            page_ids = [page['content']['id'] for page in pages]
            for page in self.get_pages_by_id(page_ids, skip_images):
                page_info = {
                    'content': page.page_content,
                    'page_id': page.metadata['id'],
                    'page_title': page.metadata['title'],
                    'page_url': page.metadata['source']
                }
                pages_info.append(page_info)
            start += self.limit
        return str(pages_info)

    def search_pages(self, query: str, skip_images: bool = False):
        """Search pages in Confluence by query text in title or page content."""
        if not self.space:
            cql = f'(type=page) and (title~"{query}" or text~"{query}")'
        else:
            cql = f'(type=page and space={self.__sanitize_confluence_space()}) and (title~"{query}" or text~"{query}")'
        return self._process_search(cql, skip_images)

    def search_by_title(self, query: str, skip_images: bool = False):
        """Search pages in Confluence by query text in title."""
        if not self.space:
            cql = f'(type=page) and (title~"{query}")'
        else:
            cql = f'(type=page and space={self.__sanitize_confluence_space()}) and (title~"{query}")'
        return self._process_search(cql, skip_images)

    def site_search(self, query: str):
        """Search for pages in Confluence using site search by query text."""
        content = []
        if not self.space:
            cql = f'(type=page) and (siteSearch~"{query}")'
        else:
            cql = f'(type=page and space={self.__sanitize_confluence_space()}) and (siteSearch~"{query}")'
        pages = self._client.cql(cql, start=0, limit=10).get("results", [])
        if not pages:
            return f"Unable to find anything using query {query}"
        # extract id, title, url and preview text
        for page in pages:
            page_data = {
                'page_id': page['content']['id'],
                'page_title': page['content']['title'],
                'page_url': page['content']['_links']['self']
            }
            page_data['preview'] = page['excerpt'] if page['excerpt'] else ""
            content.append(page_data)
        return '---'.join([str(page_data) for page_data in content])

    def process_page(self, page: dict, skip_images: bool = False) -> Document:
        if self.keep_markdown_format:
            try:
                from markdownify import markdownify
            except ImportError:
                raise ImportError(
                    "`markdownify` package not found, please run "
                    "`pip install markdownify`"
                )
        if self.include_comments or not self.keep_markdown_format:
            try:
                from bs4 import BeautifulSoup
            except ImportError:
                raise ImportError(
                    "`beautifulsoup4` package not found, please run "
                    "`pip install beautifulsoup4`"
                )
        if self.include_attachments:
            attachment_texts = self.process_attachment(page["id"], self.ocr_languages)
        else:
            attachment_texts = []

        content = self.content_format.get_content(page)
        if self.keep_markdown_format:
            # Use markdownify to keep the page Markdown style
            text = markdownify(content, heading_style="ATX") + "".join(attachment_texts)

        else:
            if self.keep_newlines:
                text = BeautifulSoup(
                    content.replace("</p>", "\n</p>").replace("<br />", "\n"), "lxml"
                ).get_text(" ") + "".join(attachment_texts)
            else:
                text = BeautifulSoup(content, "lxml").get_text(
                    " ", strip=True
                ) + "".join(attachment_texts)

        if self.include_comments:
            comments = self._client.get_page_comments(
                page["id"], expand="body.view.value", depth="all"
            )["results"]
            comment_texts = [
                BeautifulSoup(comment["body"]["view"]["value"], "lxml").get_text(
                    " ", strip=True
                )
                for comment in comments
            ]
            text = text + "".join(comment_texts)

        metadata = {
            "title": page["title"],
            "id": page["id"],
            "source": self.base_url.strip("/") + page["_links"]["webui"],
        }

        if "version" in page and "when" in page["version"]:
            metadata["when"] = page["version"]["when"]

        return Document(
            page_content=self._strip_base64_images(text) if skip_images else text,
            metadata=metadata,
        )

    def process_attachment(
            self,
            page_id: str,
            ocr_languages: Optional[str] = None,
    ) -> List[str]:
        try:
            from PIL import Image  # noqa: F401
        except ImportError:
            raise ImportError(
                "`Pillow` package not found, " "please run `pip install Pillow`"
            )

        # depending on setup you may also need to set the correct path for
        # poppler and tesseract
        attachments = self._client.get_attachments_from_content(page_id)["results"]
        texts = []
        for attachment in attachments:
            media_type = attachment["metadata"]["mediaType"]
            absolute_url = self.base_url + attachment["_links"]["download"]
            title = attachment["title"]
            try:
                if media_type == "application/pdf":
                    text = title + self.process_pdf(absolute_url, ocr_languages)
                elif (
                        media_type == "image/png"
                        or media_type == "image/jpg"
                        or media_type == "image/jpeg"
                ):
                    text = title + self.process_image(absolute_url, ocr_languages)
                elif (
                        media_type == "application/vnd.openxmlformats-officedocument"
                                      ".wordprocessingml.document"
                ):
                    text = title + self.process_doc(absolute_url)
                elif media_type == "application/vnd.ms-excel":
                    text = title + self.process_xls(absolute_url)
                elif media_type == "image/svg+xml":
                    text = title + self.process_svg(absolute_url, ocr_languages)
                else:
                    continue
                texts.append(text)
            except requests.HTTPError as e:
                if e.response.status_code == 404:
                    print(f"Attachment not found at {absolute_url}")  # noqa: T201
                    continue
                else:
                    raise

        return texts

    def execute_generic_confluence(self, method: str, relative_url: str, params: Optional[str] = "") -> str:
        """Generic Confluence Tool for Official Atlassian Confluence REST API to call, searching, creating, updating pages, etc."""
        payload_params = parse_payload_params(params)
        if method == "GET":
            response = self._client.request(
                method=method,
                path=relative_url,
                params=payload_params,
                advanced_mode=True
            )
            response_text = self.process_search_response(relative_url, response)
        else:
            response = self._client.request(
                method=method,
                path=relative_url,
                data=payload_params,
                advanced_mode=True
            )
            response_text = response.text
        response_string = f"HTTP: {method}{relative_url} -> {response.status_code}{response.reason}{response_text}"
        logger.debug(response_string)
        return response_string

    def process_search_response(self, relative_url: str, response) -> str:
        page_search_pattern = r'/rest/api/content/\d+'
        if re.match(page_search_pattern, relative_url):
            body = markdownify(response.text, heading_style="ATX")
            return body
        return response.text
    
    
    def paginate_request(self, retrieval_method: Callable, **kwargs: Any) -> List:
        """Paginate the various methods to retrieve groups of pages.

        Unfortunately, due to page size, sometimes the Confluence API
        doesn't match the limit value. If `limit` is >100 confluence
        seems to cap the response to 100. Also, due to the Atlassian Python
        package, we don't get the "next" values from the "_links" key because
        they only return the value from the result key. So here, the pagination
        starts from 0 and goes until the max_pages, getting the `limit` number
        of pages with each request. We have to manually check if there
        are more docs based on the length of the returned list of pages, rather than
        just checking for the presence of a `next` key in the response like this page
        would have you do:
        https://developer.atlassian.com/server/confluence/pagination-in-the-rest-api/

        :param retrieval_method: Function used to retrieve docs
        :type retrieval_method: callable
        :return: List of documents
        :rtype: List
        """

        max_pages = kwargs.pop("max_pages")
        docs: List[dict] = []
        next_url: str = ""
        while len(docs) < max_pages:
            get_pages = retry(
                reraise=True,
                stop=stop_after_attempt(
                    self.number_of_retries  # type: ignore[arg-type]
                ),
                wait=wait_exponential(
                    multiplier=1,
                    min=self.min_retry_seconds,  # type: ignore[arg-type]
                    max=self.max_retry_seconds,  # type: ignore[arg-type]
                ),
                before_sleep=before_sleep_log(logger, logging.WARNING),
            )(retrieval_method)
            if self.cql:  # cursor pagination for CQL
                batch, next_url = get_pages(**kwargs, next_url=next_url)
                if not next_url:
                    docs.extend(batch)
                    break
            else:
                batch = get_pages(**kwargs, start=len(docs))
                if not batch:
                    break
            docs.extend(batch)
        return docs[:max_pages]
    
    def loader(self,
            content_format: str,
            page_ids: Optional[List[str]] = None,
            label: Optional[str] = None,
            cql: Optional[str] = None,
            include_restricted_content: Optional[bool] = False,
            include_archived_content: Optional[bool] = False,
            include_attachments: Optional[bool] = False,
            include_comments: Optional[bool] = False,
            include_labels: Optional[bool] = False,
            limit: Optional[int] = 10,
            max_pages: Optional[int] = 10,
            ocr_languages: Optional[str] = None,
            keep_markdown_format: Optional[bool] = True,
            keep_newlines: Optional[bool] = True,
            bins_with_llm: bool = False,
            **kwargs) -> Generator[str, None, None]:
        """
        Loads content from Confluence based on parameters.
        Returns:
            Generator: A generator that yields content of pages that match specified criteria
        """
        from .loader import AlitaConfluenceLoader
        
        content_formant = content_format.lower() if content_format else 'view'
        mapping = {
            'view': ContentFormat.VIEW,
            'storage': ContentFormat.STORAGE,
            'export_view': ContentFormat.EXPORT_VIEW,
            'editor': ContentFormat.EDITOR,
            'anonymous': ContentFormat.ANONYMOUS_EXPORT_VIEW
        }
        content_format = mapping.get(content_formant, ContentFormat.VIEW)
        
        confluence_loader_params = {
            'url': self.base_url,
            'space_key': self.space,
            'page_ids': page_ids,
            'label': label,
            'cql': cql,
            'include_restricted_content': include_restricted_content,
            'include_archived_content': include_archived_content,
            'include_attachments': include_attachments,
            'include_comments': include_comments,
            'include_labels': include_labels,
            'content_format': content_format,
            'limit': limit,
            'max_pages': max_pages,
            'ocr_languages': ocr_languages,
            'keep_markdown_format': keep_markdown_format,
            'keep_newlines': keep_newlines,
            'min_retry_seconds': self.min_retry_seconds,
            'max_retry_seconds': self.max_retry_seconds,
            'number_of_retries': self.number_of_retries
            
        }
        
        loader = AlitaConfluenceLoader(self._client, self.llm, bins_with_llm, **confluence_loader_params)
        
        for document in loader._lazy_load(kwargs={}):
            yield document


        

    def get_available_tools(self):
        return [
            {
                "name": "create_page",
                "ref": self.create_page,
                "description": self.create_page.__doc__,
                "args_schema": createPage,
            },
            {
                "name": "create_pages",
                "ref": self.create_pages,
                "description": self.create_pages.__doc__,
                "args_schema": createPages,
            },
            {
                "name": "delete_page",
                "ref": self.delete_page,
                "description": self.delete_page.__doc__,
                "args_schema": deletePage,
            },
            {
                "name": "update_page_by_id",
                "ref": self.update_page_by_id,
                "description": self.update_page_by_id.__doc__,
                "args_schema": updatePageById,
            },
            {
                "name": "update_page_by_title",
                "ref": self.update_page_by_title,
                "description": self.update_page_by_title.__doc__,
                "args_schema": updatePageByTitle,
            },
            {
                "name": "update_pages",
                "ref": self.update_pages,
                "description": self.update_pages.__doc__,
                "args_schema": updatePages,
            },
            {
                "name": "update_labels",
                "ref": self.update_labels,
                "description": self.update_labels.__doc__,
                "args_schema": updateLabels,
            },
            {
                "name": "get_page_tree",
                "ref": self.get_page_tree,
                "description": self.get_page_tree.__doc__,
                "args_schema": getPageTree,
            },
            # {
            #     "name": "page_exists",
            #     "ref": self.page_exists,
            #     "description": self.page_exists.__doc__,
            #     "args_schema": pageExists,
            # },
            {
                "name": "get_pages_with_label",
                "ref": self.get_pages_with_label,
                "description": self.get_pages_with_label.__doc__,
                "args_schema": getPagesWithLabel,
            },
            {
                "name": "list_pages_with_label",
                "ref": self.list_pages_with_label,
                "description": self.list_pages_with_label.__doc__,
                "args_schema": getPagesWithLabel,
            },
            {
                "name": "read_page_by_id",
                "ref": self.read_page_by_id,
                "description": self.read_page_by_id.__doc__,
                "args_schema": pageId,
            },
            {
                "name": "search_pages",
                "ref": self.search_pages,
                "description": self.search_pages.__doc__,
                "args_schema": searchPages,
            },
            {
                "name": "search_by_title",
                "ref": self.search_by_title,
                "description": self.search_by_title.__doc__,
                "args_schema": searchPages,
            },
            {
                "name": "site_search",
                "ref": self.site_search,
                "description": self.site_search.__doc__,
                "args_schema": siteSearch,
            },
            {
                "name": "execute_generic_confluence",
                "description": self.execute_generic_confluence.__doc__,
                "args_schema": сonfluenceInput,
                "ref": self.execute_generic_confluence,
            },
            {
                "name": "loader",
                "ref": self.loader,
                "description": self.loader.__doc__,
                "args_schema": loaderParams,
            }
        ]