import { memo } from "react";

const COLUMN_CLASSES = {
  用例目录: "path-cell column-path",
  用例名称: "column-name",
  需求ID: "column-id",
  前置条件: "column-content",
  用例步骤: "column-content",
  预期结果: "result-cell column-content",
  用例类型: "column-option",
  用例状态: "column-status",
  用例等级: "column-status",
  创建人: "column-option",
  备注: "column-note",
  是否冒烟用例: "column-option",
  是否是冒烟用例: "column-option",
  端类型: "column-option",
  是否已经接口自动化: "column-automation",
  是否已经UI自动化: "column-automation",
};

function columnClass(header) {
  return COLUMN_CLASSES[header] || "";
}

function DataTable({ headers, rows, compact = false, emptyText = "没有匹配的用例" }) {
  if (!rows.length) return <div className="table-empty">{emptyText}</div>;
  const wideTable = headers.length > 10;

  return (
    <div className={`table-scroll ${compact ? "is-compact" : ""} ${wideTable ? "has-wide-table" : ""}`}>
      <table className={wideTable ? "wide-table" : ""}>
        <thead>
          <tr>
            <th className="row-number">#</th>
            {headers.map((header) => <th className={columnClass(header)} key={header}>{header}</th>)}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, rowIndex) => (
            <tr key={`${row["用例名称"] || "case"}-${rowIndex}`}>
              <td className="row-number">{rowIndex + 1}</td>
              {headers.map((header) => (
                <td key={header} className={columnClass(header)}>
                  {row[header] || <span className="empty-value">—</span>}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default memo(DataTable);
