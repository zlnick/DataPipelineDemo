Class demo.FHIRTransformHelper Extends %RegisteredObject
{

/// 从 FHIR endpoint 拉取指定资源类型，返回 Bundle JSON 字符串。
/// 由 demo.FHIRService 调用（Embedded Python 复用 transform_handler 模块）。
ClassMethod FetchResources(endpoint As %String, username As %String, password As %String, resourceType As %String) As %String [ Language = python ]
{
    import sys
    sys.path.insert(0, "/shared/python")

    import transform_handler
    return transform_handler.fetch_resources_json(endpoint, username, password, resourceType)
}

/// 增量拉取 FHIR 资源（基于 _lastUpdated 游标），返回 {"bundle", "max_last_updated"} JSON。
/// 由 demo.FHIRSyncService 调用。
ClassMethod FetchIncremental(endpoint As %String, username As %String, password As %String, resourceType As %String, cursor As %String) As %String [ Language = python ]
{
    import sys
    sys.path.insert(0, "/shared/python")

    import transform_handler
    return transform_handler.fetch_incremental_json(endpoint, username, password, resourceType, cursor)
}

/// 将 Bundle 中的资源逐条写入队列表 FHIRQueue（Status=pending），返回入队数量。
/// 由 demo.FHIRSyncService 调用。
ClassMethod EnqueueResources(bundleJson As %String, mappingId As %String) As %String [ Language = python ]
{
    import sys
    import importlib
    sys.path.insert(0, "/shared/python")

    # 强制重载：Embedded Python 模块缓存跨 job 共享，改文件后需 reload 才生效
    import transform_handler
    importlib.reload(transform_handler)
    return transform_handler.enqueue_resources(bundleJson, mappingId)
}

/// 按字段映射将 FHIR 资源转换为目标表行，返回 row JSON 字符串。
/// 由 demo.TransformProcess 调用。
ClassMethod TransformResource(resourceJson As %String, fieldMappingsJson As %String) As %String [ Language = python ]
{
    import sys
    sys.path.insert(0, "/shared/python")

    # 强制重载：Embedded Python 模块缓存跨 job 共享，改文件后需 reload 才生效
    import importlib
    import transform_handler
    importlib.reload(transform_handler)
    return transform_handler.transform_resource_json(resourceJson, fieldMappingsJson)
}

/// 查询目标表数据，返回行 JSON 数组字符串。
/// 由 demo.PipelineQuery.GetTargetData 调用（Embedded Python 复用 transform_handler 模块）。
ClassMethod QueryTable(tableName As %String, pLimit As %Integer = 100) As %String [ Language = python ]
{
    import sys
    sys.path.insert(0, "/shared/python")

    import transform_handler
    return transform_handler.query_table_json(tableName, pLimit)
}

/// 将行写入目标表，返回状态 JSON 字符串。
/// 由 demo.TargetOperation 调用。
ClassMethod PersistRow(tableName As %String, rowJson As %String) As %String [ Language = python ]
{
    import sys
    sys.path.insert(0, "/shared/python")

    import transform_handler
    return transform_handler.persist_row(tableName, rowJson)
}

}
