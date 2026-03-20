# Incident

- Category: memory-sync
- Time: 2026-03-12T06:13:40

## Title
Memory sync failed

## Details
memory_sync.py failed with exit code 1.     ).result
    ^
  File "C:\Users\Fernando\AppData\Roaming\Python\Python313\site-packages\qdrant_client\http\api\points_api.py", line 994, in upsert_points
    return self._build_for_upsert_points(
           ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~^
        collection_name=collection_name,
        ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
    ...<2 lines>...
        point_insert_operations=point_insert_operations,
        ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
    )
    ^
  File "C:\Users\Fernando\AppData\Roaming\Python\Python313\site-packages\qdrant_client\http\api\points_api.py", line 515, in _build_for_upsert_points
    return self.api_client.request(
           ~~~~~~~~~~~~~~~~~~~~~~~^
        type_=m.InlineResponse2005,
        ^^^^^^^^^^^^^^^^^^^^^^^^^^^
    ...<5 lines>...
        content=body,
        ^^^^^^^^^^^^^
    )
    ^
  File "C:\Users\Fernando\AppData\Roaming\Python\Python313\site-packages\qdrant_client\http\api_client.py", line 95, in request
    return self.send(request, type_)
           ~~~~~~~~~^^^^^^^^^^^^^^^^
  File "C:\Users\Fernando\AppData\Roaming\Python\Python313\site-packages\qdrant_client\http\api_client.py", line 130, in send
    raise UnexpectedResponse.for_response(response)
qdrant_client.http.exceptions.UnexpectedResponse: Unexpected Response: 400 (Bad Request)
Raw response content:
b'{"status":{"error":"Wrong input: Vector dimension error: expected dim: 768, got 1024"},"time":0.003891726}'