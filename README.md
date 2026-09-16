# restfake

A mock ArcGIS REST server that fails the way ArcGIS actually fails: 200 OK with an error body, a
token that expires mid-paging, a page that comes back short.

ArcGIS Server reports failure as HTTP 200 carrying an error envelope in the body:

```json
{"error": {"code": 500, "message": "Unable to complete operation.", "details": ["Error performing query operation"]}}
```

Every client that checks the status code and not the body treats that as success. Nobody can
write a regression test for it today, because writing one requires a broken ArcGIS Server, so the
code path that handles it is the least tested path in every GIS script ever written, including
this author's own. You find out on a Tuesday, when the nightly job reports that it copied a layer
and the layer is empty.

This is that broken server, on demand, on loopback, with the fault picked by a flag.

```
$ python restfake.py --self-test
restfake self-test: no portal, no network beyond loopback, no credentials
--------------------------------------------------------------------
PASS  an error envelope is recognised as an error
PASS  a failed addFeatures carries NO top-level error  <-- pinned defect
...
PASS  2500 rows at maxRecordCount 1000 is three pages
PASS  the three pages hold 1000, 1000 and 500 features
PASS  exceededTransferLimit reads true, true, false
PASS  and not one objectid came back twice
PASS  a layer whose size is a multiple of the page size hands back a FULL last page claiming there is more  <-- pinned defect
PASS  and the page after it is empty, which is the only honest stop condition  <-- pinned defect
PASS  an offset past the end is not an error  <-- pinned defect
PASS  asking for 5000 rows at maxRecordCount 1000 silently gives 1000 and does not fail  <-- pinned defect
PASS  returnCountOnly ignores resultOffset and counts the whole result set  <-- pinned defect
PASS  returnIdsOnly ignores maxRecordCount and returns every id  <-- pinned defect
...
PASS  and the third comes back an error  <-- pinned defect
PASS  the failure is per feature, inside a 200, inside a body that looks fine at the top level  <-- pinned defect
PASS  and stops being accepted on the third, mid-loop  <-- pinned defect
PASS  --truncate-page returns half the rows it was asked for  <-- pinned defect
PASS  --duplicate-oids makes the second page repeat 100 oids from the first  <-- pinned defect
PASS  --drop-fields removes the fields from the features  <-- pinned defect
PASS  but the LAYER DEFINITION still advertises all six  <-- pinned defect
...
PASS  and that error is a 404 code inside a 200 body  <-- pinned defect
PASS  a request with no f=json gets the html services directory page, not json  <-- pinned defect
PASS  /rest/info does not count  <-- pinned defect
PASS  the token is taken out of a logged query string  <-- pinned defect
PASS  the password is taken out of a logged post body  <-- pinned defect
PASS  and every line is flushed as it is written, because redirected to a file python buffers 8KB first and a harness server killed at the end of a CI run loses the whole log  <-- pinned defect
PASS  there is no --host flag, so nothing on the command line can move the bind off loopback  <-- pinned defect
PASS  argparse reads the word nan as a float and hands back a NaN, so --flaky cannot be range checked by its type alone  <-- pinned defect
PASS  and --flaky nan is refused as a usage error, because NaN answers False to every < and > and would otherwise reach should_flake and kill the first data request  <-- pinned defect
PASS  the harness records a false check, a missing exception, a wrong exception and an argv argparse accepted as four failures, so a broken tool turns this self-test red  <-- pinned defect
PASS  the listening socket agrees  <-- pinned defect
PASS  the FAILING request answers HTTP 200  <-- pinned defect
PASS  a client checking only the status code sees success  <-- pinned defect
PASS  a request without f=json really is served html over the wire  <-- pinned defect
PASS  the password never reached the access log  <-- pinned defect
PASS  and neither did the token  <-- pinned defect
PASS  request 2 is dropped at the transport level by --flaky 0.5, which no error envelope can express  <-- pinned defect
PASS  and the token that worked a moment ago is now refused  <-- pinned defect
PASS  a dropped request still took a request number, so retrying after a transport failure does not win its fault budget back  <-- pinned defect
PASS  no token reached stderr either, which the access log's redaction would never have caught  <-- pinned defect
PASS  and nothing at all did: the handler's default logger, which prints the whole url including the token, is really overridden  <-- pinned defect
--------------------------------------------------------------------
352 assertions, 0 failed
```

## Requirements

Python 3.9 or newer. Standard library only: `http.server`, `urllib`, `json`, `re`, `socket`,
`threading`, `time`, `html`, `io` and `argparse`. It runs on ArcGIS Pro's Python and on a plain
`python3`. No `arcpy`, no `arcgis` package, nothing to install. The same 352 assertions pass on
Windows and on Ubuntu.

```
git clone https://github.com/uhsear/restfake.git
python restfake.py --self-test
```

Most of the self-test opens no socket at all, because every response builder is a pure function
of the request parameters and the layer spec. The last two sections do open one, on 127.0.0.1
with an ephemeral port, and drive the real server with `urllib`, because a dropped connection is
not something a pure function can produce.

## Quick start

Without `--apply` nothing is bound. You get the plan first, so you can see which faults are armed
before a port opens:

```
$ python restfake.py --error-after 2
restfake: a mock ArcGIS REST server that fails the way ArcGIS actually fails
bind:   127.0.0.1:7777  (loopback only, there is no flag that changes it)
layer:  2500 row(s), maxRecordCount 1000  ->  3 page(s) for a full read
fields: OBJECTID, PARCELID, OWNER, ACRES, STATUS, LASTEDIT
faults: --error-after 2

routes:
  http://127.0.0.1:7777/rest/info?f=json
  http://127.0.0.1:7777/rest/services?f=json
  http://127.0.0.1:7777/rest/services/Parcels/FeatureServer/0?f=json
  http://127.0.0.1:7777/rest/services/Basemap/MapServer/0?f=json
  http://127.0.0.1:7777/rest/services/Parcels/FeatureServer/0/query?where=1%3D1&outFields=*&f=json
  http://127.0.0.1:7777/rest/generateToken (POST username, password)

Check only. No socket was opened. Re-run with --apply to serve.
```

Add `--apply` and point your code at it:

```
python restfake.py --apply --error-after 2
```

```
$ Q="http://127.0.0.1:7777/rest/services/Parcels/FeatureServer/0/query?where=1=1&f=json"
$ for i in 1 2 3; do curl -s "$Q" -o body.json -w "  <- HTTP %{http_code}\n"; head -c 100 body.json; echo; done
  <- HTTP 200
{"objectIdFieldName": "OBJECTID", "uniqueIdField": {"name": "OBJECTID", "isSystemMaintained": true},
  <- HTTP 200
{"objectIdFieldName": "OBJECTID", "uniqueIdField": {"name": "OBJECTID", "isSystemMaintained": true},
  <- HTTP 200
{"error": {"code": 500, "message": "Unable to complete operation.", "details": ["Error performing qu
```

Three requests. Three 200s. One of them failed.

## The fault flags

This is the product. Everything else is scenery.

| Flag | Default | What it does |
|---|---|---|
| `--error-after N` | off | Answer the first N data requests normally, then return HTTP 200 with an error envelope. |
| `--token-expires-after N` | off | Require a token, and stop accepting the issued one after N data requests. |
| `--truncate-page` | off | Return half the features a full page promised, with `exceededTransferLimit` still true. |
| `--duplicate-oids` | off | Start each page before the previous one ended, so OBJECTIDs repeat across pages. |
| `--drop-fields F,G` | none | Leave fields out of query responses while the layer definition still advertises them. |
| `--slow MS` | `0` | Delay every response by this many milliseconds. |
| `--flaky RATE` | `0` | Drop this fraction of data requests at the transport level, deterministically. |

A data request is a `/query` or an `/addFeatures`. Metadata and `/generateToken` are not counted,
so `--error-after 2` means the same request whatever your client read first. A request that was
refused or dropped still takes a number: retrying after a transport failure does not win its
fault budget back, which is the same arithmetic a real server's rate limiter does.

**`--error-after`** is the headline. On `/query` it is a top-level error envelope with no
`features` key at all, so a client that ignored the body fails on the next line instead of
shipping an empty layer. On `/addFeatures` it takes the nastier shape the real server uses: HTTP
200, no top-level error, and `"success": false` inside each per-feature result. A client that
checks the status code and then checks for a top-level error still sees nothing wrong, and
reports that it wrote rows it did not write.

```
$ python restfake.py --apply --error-after 2
...
#1 GET .../0/query?where=1=1&resultRecordCount=2&f=json 200 2 feature(s) more
#2 GET .../0/query?where=1=1&resultRecordCount=2&f=json 200 2 feature(s) more
#3 GET .../0/query?where=1=1&resultRecordCount=2&f=json 200 ERROR 500 Unable to complete operation.
#4 GET .../0/query?where=1=1&resultRecordCount=2&f=json 200 ERROR 500 Unable to complete operation.
#5 POST .../0/addFeatures?features=[{"attributes": {"OWNER": "NEW"}}]&f=json 200 0/1 added
```

**`--truncate-page`** separates the two paging loops that look identical until a page comes back
short. Only a full page is truncated, so the layer can still be read to the end and the fault
stays a paging bug rather than a layer nobody can download. Driven for real against 2500 rows:

```
a loop advancing by resultRecordCount read 1500 of 2500 rows
a loop advancing by len(features) read 2500 of 2500 rows, 2500 distinct
```

**`--duplicate-oids`** answers the other half of that question. A client that concatenates pages
without deduplicating gets 1900 rows out of a 1900 row read and 100 of them twice:

```
page 1 -> 1000 feature(s), oids 1..1000
page 2 -> 1000 feature(s), oids 901..1900
overlap -> 100 oid(s) repeated: [901, 902, 903, 904, 905] ...
```

**`--drop-fields`** is a service that was not restarted after somebody deleted a column. The
layer definition is the promise and it stays intact; the data stops keeping it:

```
layer definition advertises -> ['OBJECTID', 'PARCELID', 'OWNER', 'ACRES', 'STATUS', 'LASTEDIT']
query actually returns      -> ['LASTEDIT', 'OBJECTID', 'PARCELID', 'STATUS']
```

Code that builds a schema from the layer definition and then reads `row["attributes"]["OWNER"]`
raises `KeyError` on the first row. Code that writes that into a shapefile writes nulls forever.

**`--flaky`** is deterministic and not a coin toss. Request N drops when
`int(N*rate) > int((N-1)*rate)`, so `--flaky 0.5` drops requests 2, 4 and 6 on every run and a
failing CI job replays exactly. A test suite whose failures land somewhere different each time is
the thing this tool exists to stop people shipping.

```
request 1 -> HTTP 200 ok
request 2 -> TRANSPORT FAILURE: RemoteDisconnected('Remote end closed connection without response')
request 3 -> HTTP 200 ok
request 4 -> TRANSPORT FAILURE: RemoteDisconnected('Remote end closed connection without response')
```

**`--token-expires-after`** arms token security as well as expiry, so an anonymous request is
refused with code 499 and the issued token is refused with 498 once its budget runs out. Both
arrive inside a 200. `/rest/info` reports `isTokenBasedSecurity: true` once it is armed, so a
client picks its sign-in branch.

## What it serves

| Route | What comes back |
|---|---|
| `/rest/info` | `currentVersion`, `fullVersion`, `authInfo` with the token service url. |
| `/rest/services` | A catalog: `Parcels` as a FeatureServer, `Basemap` as a MapServer. |
| `/rest/services/<name>/<type>` | The service description, its `maxRecordCount` and its one layer. |
| `/rest/services/<name>/<type>/0` | The layer definition: `objectIdField`, `fields`, `extent`, `supportsPagination`. |
| `.../0/query` | `where`, `outFields`, `returnGeometry`, `returnCountOnly`, `returnIdsOnly`, `resultOffset`, `resultRecordCount`. |
| `.../0/addFeatures` | `addResults`, one per feature. FeatureServer only. |
| `/rest/generateToken` | A token, for any non-empty username and password. |

A web adaptor prefix is ignored, so `/arcgis/rest/services/...` routes the same as
`/rest/services/...` and a url copied off a real server works unchanged.

The layer holds 2500 deterministic rows of `OBJECTID`, `PARCELID`, `OWNER`, `ACRES`, `STATUS` and
`LASTEDIT`, with point geometry in EPSG:2881. `--rows` and `--max-record-count` change the shape
of the paging problem; the same numbers always build the same rows, so a failing test replays.

| Flag | Default | What it does |
|---|---|---|
| `--port` | `7777` | Loopback port to serve on. |
| `--rows` | `2500` | Rows in the fake layer. |
| `--max-record-count` | `1000` | Rows the layer will return in one page. |
| `--apply` | off | Open the socket and serve. Without it the plan is printed and no port is bound. |
| `--self-test` | off | Run the assertions and exit. |

Exit codes: 0 the plan was printed or the server stopped, 2 the bind failed, 64 usage error.

## Why it only answers on 127.0.0.1

The bind address is a constant in the file, not a flag, and `--self-test` asserts that argparse
refuses both `--host` and `--bind` so that nothing on a command line can move it.

A fake portal that answers on the LAN is an attractive nuisance. It serves a catalog that looks
like a real one, it hands a token to anybody who asks for one, and the entire purpose of it is to
return wrong answers. Somebody else's script finding it on a shared network, or a colleague
pointing a test at what they think is staging, is a failure mode with no upside whatsoever. If
you need it reachable from a container, forward the loopback port deliberately; do not make a
wide bind the default anybody can trip over.

Proven on the box rather than argued about:

```
non-loopback addresses on this host: ['192.168.0.248']
127.0.0.1:7802         -> HTTP 200, the server is up
192.168.0.248:7802     -> connect_ex 10061 (REFUSED, not listening here)
netstat for port 7802:
  TCP    127.0.0.1:7802         0.0.0.0:0              LISTENING       24776
```

## The credentials it is given

The password arrives in a POST body and the token arrives in the query string, which is the exact
text an access log would otherwise echo. Both are replaced with `[redacted]` before the line is
built, along with `pwd`, `client_secret`, `refresh_token` and `code`. The username is kept,
because a log that hides everything is not a log.

```
   POST /rest/generateToken?username=gis_admin&password=[redacted]&f=json 200 token issued
#2 GET .../0/query?where=1=1&token=[redacted]&f=json 200 2 feature(s) more
```

The tokens this server issues are a fixed string with no secret in it, so a leak of one costs
nothing. That is not a reason to leak it, and nine server logs from a full exercise of every flag
were grepped for both the token and the password sent on the wire. Neither appears in any of
them. The self-test collects `stderr` across the exchange that carries a token and asserts it
stayed empty, because `http.server`'s own logger prints the whole url and would put the token
somewhere the access log's redaction never reaches.

## The CI harness the other tools do not have

`fullpull`, `hostedreap` and `agol-relink` all page an ArcGIS REST endpoint and all handle the
200-with-an-error-body case, and not one of them has a test that proves it. They cannot have one.
The test needs a server that fails on demand, and until now the only way to get a failing ArcGIS
Server was to wait for one.

Point them here instead:

```
python restfake.py --apply --error-after 3 &
fullpull --url http://127.0.0.1:7777/rest/services/Parcels/FeatureServer/0 --out parcels.gpkg
```

The assertion is not that the tool succeeds. It is that a tool which cannot finish says so and
exits non-zero, instead of writing a short file and reporting success. `--truncate-page` and
`--duplicate-oids` ask the same question of the paging loop, `--drop-fields` asks it of the
schema handling, and `--flaky` asks it of the retry logic. Every one of them is a bug that has
shipped in this author's code at least once.

## Why not the tools that already exist

`unittest.mock` patching `requests.get` is the obvious answer and it is the right one for a
single call. It stops being the right one at a paging loop, because the mock has to carry the
paging state, and now the thing under test and the thing testing it share an author and a
misunderstanding. A mock built by somebody who thinks `exceededTransferLimit` means "rows
remain" will happily agree with a client that thinks the same, and both are wrong.

`responses` and `requests-mock` are better at this and are the tools to reach for if your client
uses `requests`. They still intercept at the library boundary, so they cannot drop a connection,
they cannot be slow in a way a socket timeout notices, and a client that uses `urllib` or `arcgis`
instead does not go through them at all. This is a real HTTP server on a real socket, so whatever
your client speaks reaches it.

A recorded fixture, VCR style, is accurate about the one day it was recorded. It cannot be asked
for the failure you have never seen, which is the failure you need to test.

Esri's own tooling has no answer here. There is no local ArcGIS Server, the developer edition is
a licensed install, and neither has a switch marked "fail the third request".

## Limits

- The where parser handles one comparison: `=`, `<>`, `!=`, `>`, `>=`, `<`, `<=`, `LIKE`,
  `NOT LIKE` and `IN`. No `AND`, no `OR`, no parentheses and no functions. Splitting on `" AND "`
  breaks on a string literal containing the word, and a real SQL parser is a week of work for a
  fixture layer.
- `LIKE` is case sensitive here. ArcGIS on SQL Server is not, under the usual collation, so a
  clause that matches against production can match nothing against this.
- No geometry filtering. `geometry`, `spatialRel` and `inSR` are accepted and ignored, so a
  spatial query returns the whole layer. A client under test that relies on the server filtering
  spatially will see more rows than it expects, which is a different bug from the one you came
  for.
- `addFeatures` acknowledges and discards. The row count does not change and a later query does
  not see what you added. This is a harness for error handling, not a database, and keeping the
  writes would make the fault behaviour depend on the order your tests happen to run in.
- `updateFeatures`, `deleteFeatures`, `applyEdits` and `queryRelatedRecords` are not implemented.
  They answer with a 404 inside a 200, like any other unknown operation.
- One layer per service, always id 0, and both services share the same rows.
- `exceededTransferLimit` is always present. Some ArcGIS releases omit the key when it is false,
  which turns `response["exceededTransferLimit"]` into a `KeyError` against a real server that
  this fake will not reproduce.
- No HTTPS. A certificate a client must be told to trust is a second problem, and every fault
  here is above the transport. If you are testing certificate handling, this is the wrong tool.
- `--slow` delays every response equally. A real server is slow on the queries that scan and fast
  on the ones that seek.
- It is not a load generator. Requests are served on a thread each and the row set lives in
  memory, so `--rows 5000000` will simply use the memory.
- The fault counter is process wide, not per client. Two test processes against one server share
  a budget and will confuse each other. Give each test its own port.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

Other single-file tools in this portfolio that pair with this one:

- [fullpull](https://github.com/uhsear/fullpull) - the paging client this was built to break, on purpose, before production does
- [hostedreap](https://github.com/uhsear/hostedreap) - rehearse a delete against a fake service first
- [agol-relink](https://github.com/uhsear/agol-relink) - the third client that never had a server to test against
- [svcdrift](https://github.com/uhsear/svcdrift) - a schema diff to run against a service that lies about its fields
