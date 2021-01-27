# pylint: disable=protected-access
import singer
import json
import singer.utils as singer_utils

from requests.exceptions import HTTPError
from tap_quickbooks.quickbooks.exceptions import TapQuickbooksException

LOGGER = singer.get_logger()

MAX_RETRIES = 4

class Rest():

    def __init__(self, qb):
        self.qb = qb

    def query(self, catalog_entry, state):
        start_date = self.qb.get_start_date(state, catalog_entry)
        query = self.qb._build_query_string(catalog_entry, start_date)

        return self._query_recur(query, catalog_entry, start_date)

    # pylint: disable=too-many-arguments
    def _query_recur(
            self,
            query,
            catalog_entry,
            start_date_str,
            end_date=None,
            retries=MAX_RETRIES):
        params = {
            "query": query,
            "minorversion": "45"
        }
        url = f"{self.qb.instance_url}/query"
        headers = self.qb._get_standard_headers()

        sync_start = singer_utils.now()
        if end_date is None:
            end_date = sync_start

        if retries == 0:
            raise TapQuickbooksException(
                "Ran out of retries attempting to query Quickbooks Object {}".format(
                    catalog_entry['stream']))

        retryable = False
        try:
            for rec in self._sync_records(url, headers, params, catalog_entry['stream']):
                yield rec

            # If the date range was chunked (an end_date was passed), sync
            # from the end_date -> now
            if end_date < sync_start:
                next_start_date_str = singer_utils.strftime(end_date)
                query = self.qb._build_query_string(catalog_entry, next_start_date_str)
                for record in self._query_recur(
                        query,
                        catalog_entry,
                        next_start_date_str,
                        retries=retries):
                    yield record

        except HTTPError as ex:
            response = ex.response.json()
            if isinstance(response, list) and response[0].get("errorCode") == "QUERY_TIMEOUT":
                start_date = singer_utils.strptime_with_tz(start_date_str)
                day_range = (end_date - start_date).days
                LOGGER.info(
                    "Quickbooks returned QUERY_TIMEOUT querying %d days of %s",
                    day_range,
                    catalog_entry['stream'])
                retryable = True
            else:
                raise ex

        if retryable:
            start_date = singer_utils.strptime_with_tz(start_date_str)
            half_day_range = (end_date - start_date) // 2
            end_date = end_date - half_day_range

            if half_day_range.days == 0:
                raise TapQuickbooksException(
                    "Attempting to query by 0 day range, this would cause infinite looping.")

            query = self.qb._build_query_string(catalog_entry, singer_utils.strftime(start_date),
                                                singer_utils.strftime(end_date))
            for record in self._query_recur(
                    query,
                    catalog_entry,
                    start_date_str,
                    end_date,
                    retries - 1):
                yield record

    def _sync_records(self, url, headers, params, stream):
        headers["Accept"] = "application/json"
        headers["Content-Type"] = "application/json"

        query = params['query']
        offset = 0;
        max = 100
        while True:
            params['query'] = f"{query}  STARTPOSITION {offset} MAXRESULTS {max}"
            resp = self.qb._make_request('GET', url, headers=headers, params=params)
            resp_json = resp.json()

            # Establish number of records returned.
            count = resp_json['QueryResponse'].get('maxResults', 0)

            # Make sure there is alteast one record.
            if count == 0:
                break;

            records = resp_json['QueryResponse'][stream];

            for i, rec in enumerate(records):
                yield rec

            # This is was chunk
            if count < max:
                break;

            offset += max
