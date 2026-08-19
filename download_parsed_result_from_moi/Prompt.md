# 需求概要
写 [Download.py](./Download.py)，要求用户输入 Authorization、x-workspace-id、volume_id、page_size、，实现下载 MOI 平台指定工作区和卷下的文件，并经过结构编排，放到 [图纸](../图纸/) 目录下。

# 下载步骤
1. 调用 https://backend-zhongding.moi.shanghai.idc.matrixorigin.cn:30443/newmoi/catalog/file/list 文件列表接口，接口参数如下：
- 请求方式
post
- 请求头
{Authorization:"XXX",
x-workspace-id:"XXX",
Content-Type:"application/json"
}
- 请求体样例
{"page":1,"page_size":400,"order":"desc","filters":[{"name":"volume_id","values":["5"]},{"name":"parent_id","values":[""]}]}
2. 根据上面步骤 1 返回的 json 数组，见[文件列表接口返回样例](#Sample)，对每个 id（即文件编码），调用 https://backend-zhongding.moi.shanghai.idc.matrixorigin.cn:30443/newmoi/catalog/file/download 文件下载接口，接口参数如下：

- 请求方式
post
- 请求头
{Authorization:"XXX",
x-workspace-id:"XXX"
}
- 请求体样例
{"volume_id":"5","file_id":"2656d588-3b13-45fa-a136-68696f4042ba"}

将每次下载内容转换为一个 zip 文件，解压后会有两个文件，一个目录，如：20C114319.pdf_parse.json、20C114319.pdf.md、images 目录（内含文件）。对结构进行编排，使得最终组装成一个目录 20C114319，该目下含：20C114319_extracted.md 文件和一个子目录 20C114319_assets，该子目录下含 images 目录（内含文件），最终将目录 20C114319 放置在项目目录下的[图纸](../图纸/)目录中。


<a id="Sample"></a>
```
文件列表接口返回样例
{
    "code": "OK",
    "data": {
        "total": 211,
        "list": [
            {
                "id": "2656d588-3b13-45fa-a136-68696f4042ba",
                "name": "20C114319.pdf_26977c07-bf89-4ddf-912f-c694019e39b2.zip",
                "file_type": "file",
                "workflow_role": "",
                "file_ext": "zip",
                "origin_file_name": "",
                "origin_file_ext": "",
                "ref_file_id": "",
                "parsed_file_id": "",
                "size": 3371456,
                "parent_id": "",
                "created_at": "2026-06-18T17:20:18+08:00",
                "created_by": "admin",
                "volume_id": "5",
                "volume_name": "",
                "volume_reserved": false,
                "ref_workflow_id": ""
            },
            {
                "id": "16444687-cf7e-4def-9dc2-92e2f0bdd01c",
                "name": "20C114307.pdf_1298e697-9b69-4123-b092-d280cc308222.zip",
                "file_type": "file",
                "workflow_role": "",
                "file_ext": "zip",
                "origin_file_name": "",
                "origin_file_ext": "",
                "ref_file_id": "",
                "parsed_file_id": "",
                "size": 2734375,
                "parent_id": "",
                "created_at": "2026-06-18T16:59:05+08:00",
                "created_by": "admin",
                "volume_id": "5",
                "volume_name": "",
                "volume_reserved": false,
                "ref_workflow_id": ""
            }
        ]
    }
}
```