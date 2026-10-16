import { Check } from "lucide-react";

const STEPS = [
  ["1", "配置转换", "方向与规则"],
  ["2", "核对内容", "表格与节点"],
  ["3", "导出结果", "XLSX / XMind"],
];

export default function StepRail({ activeStep, children }) {
  return (
    <div className="step-rail">
      <nav className="step-list" aria-label="转换步骤">
        {STEPS.map(([number, label, description], index) => {
          const step = index + 1;
          const completed = step < activeStep;
          return (
            <div className={`step-item ${step === activeStep ? "is-active" : ""} ${completed ? "is-complete" : ""}`} key={number}>
              <span className="step-number">{completed ? <Check size={15} /> : number}</span>
              <span>
                <strong>{label}</strong>
                <small>{description}</small>
              </span>
            </div>
          );
        })}
      </nav>
      {children ? <div className="step-actions">{children}</div> : null}
    </div>
  );
}
