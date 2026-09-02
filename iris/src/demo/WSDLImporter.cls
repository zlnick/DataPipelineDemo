Class demo.WSDLImporter Extends %RegisteredObject
{

/// SOAP 目标接入（WSDL 导入型）：读 WSDL → %SOAP.WSDL.Reader 生成
/// proxy client + 消息类 + Business Operation，BO 类作为管道 Operation 组件。
///
/// 关键条件（已验证，见知识库 SOAP-WSDL反向生成BO.md）：
///   MakeClient=1、MakeBusinessOperation=1、MakeEnsembleClasses 必须保持默认 0
///   （源码 If ..MakeEnsembleClasses {} ElseIf ..MakeBusinessOperation {}，设 1 会走 Ensemble 分支跳过 BO）。
///
/// 参数:
///   wsdl: WSDL 地址（http/https URL 或本地文件路径）
///   clientPackage / boPackage / msgPackage: 生成类所在包
///
/// 返回: JSON {"ok":bool,"boClass":"生成的 BO 类名","message":错误文本}
ClassMethod Import(wsdl As %String,
                   clientPackage As %String = "demo.soap.client",
                   boPackage As %String = "demo.soap.bo",
                   msgPackage As %String = "demo.soap.msg") As %String
{
    Set tReader = ##class(%SOAP.WSDL.Reader).%New()
    Set tReader.ClientPackage = clientPackage
    Set tReader.BusinessOperationPackage = boPackage
    Set tReader.OperationPackage = msgPackage
    Set tReader.RequestPackage = msgPackage
    Set tReader.ResponsePackage = msgPackage
    Set tReader.MakeClient = 1
    Set tReader.MakeBusinessOperation = 1
    // MakeEnsembleClasses 保持默认 0

    // %SOAP.WSDL.Reader.Process 直接接受 URL 或文件路径（唯一读取入口）
    Set tSC = tReader.Process(wsdl)
    If $$$ISERR(tSC) {
        Set tObj = {"ok":0, "message":($system.Status.GetErrorText(tSC))}
        Quit tObj.%ToJSON()
    }

    // 查找生成的 BO 类：继承 Ens.BusinessOperation 且包名包含 boPackage。
    // 注意：BO 类名由 IRIS 拼接（如 PatientService.demo.soap.bont.PatientServicePort），
    // 前缀为 WSDL 服务名，不能直接推算，只能按类型特征查询。
    Set tBoClass = ""
    Set tRS = ##class(%SQL.Statement).%New()
    Set tSC2 = tRS.%Prepare(
        "SELECT TOP 1 Name FROM %Dictionary.CompiledClass " _
        "WHERE Super LIKE '%Ens.BusinessOperation%' AND Name LIKE ? " _
        "ORDER BY Name DESC")
    If $$$ISOK(tSC2) {
        Set tRset = tRS.%Execute("%" _ boPackage _ "%")
        If tRset.%Next() {
            Set tBoClass = tRset.%Get("Name")
        }
    }
    If tBoClass = "" {
        Set tObj = {"ok":0, "message":("WSDL 导入完成但未找到生成的 BO 类（包 "_boPackage_"）")}
        Quit tObj.%ToJSON()
    }

    Set tEntities = ..InspectEntities(boPackage, clientPackage)
    Quit {"ok":1, "boClass":(tBoClass), "entities":(##class(%DynamicAbstractObject).%FromJSON(tEntities))}.%ToJSON()
}

/// 分析由 WSDL 生成的相关实体类及其字段属性
ClassMethod InspectEntities(boPackage As %String = "demo.soap.bo",
                            clientPackage As %String = "demo.soap.client") As %String
{
    Set tArr = []
    Set tRS = ##class(%SQL.Statement).%New()
    Set tSC = tRS.%Prepare(
        "SELECT parent, Name, Type FROM %Dictionary.CompiledProperty " _
        "WHERE (parent LIKE ? OR parent LIKE ? OR parent LIKE '%PatientService%') " _
        "AND Name NOT LIKE '%%' " _
        "ORDER BY parent, Name")
    If $$$ISOK(tSC) {
        Set tRset = tRS.%Execute("%" _ boPackage _ "%", "%" _ clientPackage _ "%")
        Set tCurrentClass = ""
        Set tCols = []
        Set tFields = []
        While tRset.%Next() {
            Set tClass = tRset.%Get("parent")
            // 过滤系统/框架类
            If (tClass [ "bont.") || (tClass [ ".client.") Continue
            
            Set tPropName = tRset.%Get("Name")
            // 过滤系统框架属性
            If (tPropName = "Adapter") || (tPropName = "AlertOnError") || (tPropName = "RetryInterval") Continue

            If (tCurrentClass '= "") && (tClass '= tCurrentClass) {
                Set tEntityName = $Piece(tCurrentClass, ".", *)
                If (tCols.%Size() > 0) {
                    Do tArr.%Push({
                        "entity_name": (tEntityName),
                        "table": (tEntityName),
                        "schema": "SOAP",
                        "full_class": (tCurrentClass),
                        "columns": (tCols),
                        "fields": (tFields)
                    })
                }
                Set tCols = []
                Set tFields = []
            }
            Set tCurrentClass = tClass
            Set tPropType = tRset.%Get("Type")
            Set tSimpleType = $Piece(tPropType, ".", *)
            If (tSimpleType [ "String") { Set tSimpleType = "string" }
            ElseIf (tSimpleType [ "Integer") { Set tSimpleType = "integer" }
            ElseIf (tSimpleType [ "Boolean") { Set tSimpleType = "boolean" }

            Set tItem = {}
            Set tItem.name = tPropName
            Set tItem.type = tSimpleType
            Do tCols.%Push(tItem)
            Do tFields.%Push(tPropName)
        }
        If (tCurrentClass '= "") && (tCols.%Size() > 0) {
            Set tEntityName = $Piece(tCurrentClass, ".", *)
            // 避免重复
            Set tFound = 0
            For i=0:1:tArr.%Size()-1 {
                If tArr.%Get(i).entity_name = tEntityName Set tFound = 1 Quit
            }
            If 'tFound {
                Do tArr.%Push({
                    "entity_name": (tEntityName),
                    "table": (tEntityName),
                    "schema": "SOAP",
                    "full_class": (tCurrentClass),
                    "columns": (tCols),
                    "fields": (tFields)
                })
            }
        }
    }
    Quit tArr.%ToJSON()
}

}
