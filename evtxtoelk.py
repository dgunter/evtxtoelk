import contextlib
import mmap
import traceback
import json
import argparse
from collections import OrderedDict
from datetime import datetime

from Evtx.Evtx import FileHeader
from Evtx.Views import evtx_file_xml_view
from elasticsearch import Elasticsearch
import xmltodict


class EvtxToElk:
    @staticmethod
    def evtx_to_elk(filename, elk_ip):
        es = Elasticsearch([elk_ip])
        with open(filename) as infile:
            with contextlib.closing(mmap.mmap(infile.fileno(), 0, access=mmap.ACCESS_READ)) as buf:
                fh = FileHeader(buf, 0x0)
                data = ""
                for xml, record in evtx_file_xml_view(fh):
                    try:
                        contains_event_data = False
                        log_line = xmltodict.parse(xml)

                        # Format the date field
                        date = log_line.get("Event").get("System").get("TimeCreated").get("@SystemTime")
                        if "." not in str(date):
                            date = datetime.strptime(date, "%Y-%m-%d %H:%M:%S")
                        else:
                            date = datetime.strptime(date, "%Y-%m-%d %H:%M:%S.%f")
                        log_line['@timestamp'] = str(date.isoformat())
                        log_line["Event"]["System"]["TimeCreated"]["@SystemTime"] = str(date.isoformat())

                        # Process the data field to be searchable
                        data = ""
                        if log_line.get("Event") is not None:
                            data = log_line.get("Event")
                            if log_line.get("Event").get("EventData") is not None:
                                data = log_line.get("Event").get("EventData")
                                if log_line.get("Event").get("EventData").get("Data") is not None:
                                    data = log_line.get("Event").get("EventData").get("Data")
                                    if isinstance(data, list):
                                        contains_event_data = True
                                        data_vals = {}
                                        for dataitem in data:
                                            try:
                                                if dataitem.get("@Name") is not None:
                                                    data_vals[str(dataitem.get("@Name"))] = str(
                                                        str(dataitem.get("#text")))
                                            except:
                                                pass
                                        log_line["Event"]["EventData"]["Data"] = data_vals
                                    else:
                                        if isinstance(data, OrderedDict):
                                            log_line["Event"]["EventData"]["RawData"] = json.dumps(data)
                                        else:
                                            log_line["Event"]["EventData"]["RawData"] = str(data)
                                        del log_line["Event"]["EventData"]["Data"]
                                else:
                                    if isinstance(data, OrderedDict):
                                        log_line["Event"]["RawData"] = json.dumps(data)
                                    else:
                                        log_line["Event"]["RawData"] = str(data)
                                    del log_line["Event"]["EventData"]
                            else:
                                if isinstance(data, OrderedDict):
                                    log_line = dict(data)
                                else:
                                    log_line["RawData"] = str(data)
                                    del log_line["Event"]
                        else:
                            pass

                        # Insert data into ElasticSearch
                        es.index(index="hostlogs", doc_type="hostlogs", body=json.loads(json.dumps(log_line)))
                    except:
                        print("***********")
                        print("Parsing Exception")
                        print(traceback.print_exc())
                        print(json.dumps(log_line, indent=2))
                        print("***********")


if __name__ == "__main__":
    # Create argument parser
    parser = argparse.ArgumentParser()
    # Add arguments
    parser.add_argument('evtxfile', help="Evtx file to parse")
    parser.add_argument('elk_ip', default="localhost", help="IP (and port) of ELK instance")
    # Parse arguments and call evtx to elk class
    args = parser.parse_args()
    EvtxToElk.evtx_to_elk(args.evtxfile, args.elk_ip)
