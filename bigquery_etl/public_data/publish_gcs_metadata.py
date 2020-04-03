"""Generate and upload JSON metadata files for public datasets on GCS."""

from argparse import ArgumentParser
import json
import logging
import os
import re
import smart_open

from google.cloud import storage
from itertools import groupby

from ..parse_metadata import Metadata
from ..util import standard_args


DEFAULT_BUCKET = "mozilla-public-data-http"
DEFAULT_API_VERSION = "v1"
DEFAULT_ENDPOINT = "http.public-data.prod.dataops.mozgcp.net/"
REVIEW_LINK = "https://bugzilla.mozilla.org/show_bug.cgi?id="
GCS_FILE_PATH_RE = re.compile(
    r"api/(?P<api_version>.+)/tables/(?P<dataset>.+)/(?P<table>.+)/(?P<version>.+)/"
    r"files/(?:(?P<date>.+)/)?(?P<filename>.+\.json\.gz)"
)

parser = ArgumentParser(description=__doc__)
parser.add_argument(
    "--project_id", "--project-id", default="mozilla-public-data", help="Target project"
)
parser.add_argument(
    "--target-bucket",
    "--target_bucket",
    default=DEFAULT_BUCKET,
    help="GCP bucket JSON data is exported to",
)
parser.add_argument(
    "--endpoint", default=DEFAULT_ENDPOINT, help="The URL to access the HTTP endpoint"
)
parser.add_argument(
    "--api_version",
    "--api-version",
    default=DEFAULT_API_VERSION,
    help="Endpoint API version",
)
parser.add_argument(
    "--target", help="File or directory containing metadata files", default="sql/"
)
standard_args.add_log_level(parser)


class GcsTableMetadata:
    """Metadata associated with table data stored on GCS."""

    def __init__(self, files, endpoint, target_dir):
        """Initialize container for metadata of a table published on GCS."""
        assert len(files) > 0
        self.files = files
        self.endpoint = endpoint
        self.files_path = self.files[0].split("files")[0] + "files"
        self.files_uri = endpoint + self.files_path

        (self.dataset, self.table, self.version) = dataset_table_version_from_gcs_path(
            self.files[0]
        )
        self.metadata = Metadata.of_table(
            self.dataset, self.table, self.version, target_dir
        )

    def table_metadata_to_json(self):
        """Return a JSON object of the table metadata for GCS."""
        metadata_json = {}
        metadata_json["friendly_name"] = self.metadata.friendly_name
        metadata_json["description"] = self.metadata.description
        metadata_json["incremental"] = self.metadata.is_incremental()
        metadata_json["incremental_export"] = self.metadata.is_incremental_export()

        if self.metadata.review_bug() is not None:
            metadata_json["review_link"] = REVIEW_LINK + self.metadata.review_bug()

        metadata_json["files_uri"] = self.files_uri
        # todo: add last updated

        return metadata_json

    def files_metadata_to_json(self):
        """Return a JSON object containing metadata of the files on GCS."""
        if self.metadata.is_incremental_export():
            metadata_json = {}

            for file in self.files:
                match = GCS_FILE_PATH_RE.match(file)
                date = match.group("date")

                if date is not None:
                    if date in metadata_json:
                        metadata_json[date].append(file)
                    else:
                        metadata_json[date] = [file]

            return metadata_json
        else:
            return self.files


def dataset_table_version_from_gcs_path(gcs_path):
    """Extract the dataset, table and version from the provided GCS blob path."""
    match = GCS_FILE_PATH_RE.match(gcs_path)

    if match is not None:
        return (match.group("dataset"), match.group("table"), match.group("version"))
    else:
        return None


def get_public_gcs_table_metadata(
    storage_client, bucket, api_version, endpoint, target_dir
):
    """Return a list of metadata of public tables and their locations on GCS."""
    prefix = f"api/{api_version}"

    blobs = storage_client.list_blobs(bucket, prefix=prefix)
    blob_paths = [blob.name for blob in blobs]

    return [
        GcsTableMetadata(list(files), endpoint, target_dir)
        for table, files in groupby(blob_paths, dataset_table_version_from_gcs_path)
        if table is not None
    ]


def publish_all_datasets_metadata(table_metadata, output_file):
    """Write metadata about all available public datasets to GCS."""
    metadata_json = {}

    for metadata in table_metadata:
        if metadata.dataset not in metadata_json:
            metadata_json[metadata.dataset] = {}

        dataset = metadata_json[metadata.dataset]

        if metadata.table not in dataset:
            dataset[metadata.table] = {}

        table = dataset[metadata.table]

        if metadata.version not in table:
            table[metadata.version] = {}

        table[metadata.version] = metadata.table_metadata_to_json()

    logging.info(f"Write metadata to {output_file}")

    with smart_open.open(output_file, "w") as fout:
        fout.write(json.dumps(metadata_json))


def publish_table_metadata(table_metadata, bucket):
    """Write metadata for each public table to GCS."""
    for metadata in table_metadata:
        output_file = f"gs://{bucket}/{metadata.files_path}/metadata.json"

        logging.info(f"Write metadata to {output_file}")
        with smart_open.open(output_file, "w") as fout:
            fout.write(json.dumps(metadata.files_metadata_to_json()))


def main():
    """Generate and upload GCS metadata."""
    args = parser.parse_args()
    storage_client = storage.Client(args.project_id)

    # set log level
    try:
        logging.basicConfig(level=args.log_level, format="%(levelname)s %(message)s")
    except ValueError as e:
        parser.error(f"argument --log-level: {e}")

    if os.path.isdir(args.target):
        gcs_table_metadata = get_public_gcs_table_metadata(
            storage_client,
            args.target_bucket,
            args.api_version,
            args.endpoint,
            args.target,
        )

        output_file = f"gs://{args.target_bucket}/all_datasets.json"
        publish_all_datasets_metadata(gcs_table_metadata, output_file)
        publish_table_metadata(gcs_table_metadata, args.target_bucket)
    else:
        print(
            """
            Invalid target: {}, target must be a directory with
            structure /<dataset>/<table>/metadata.yaml.
            """.format(
                args.target
            )
        )


if __name__ == "__main__":
    main()
