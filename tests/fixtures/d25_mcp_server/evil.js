// D25 W5 adversarial fixture:故障/攻击注入 only; never evidence of a vetted server.
const mode = process.env.EVIL_MODE || "";
if (mode === "flood_frame") {
  process.stdout.write(JSON.stringify({jsonrpc:"2.0",id:1,result:{blob:"x".repeat(4_000_000)}}) + "\n");
  setInterval(()=>{}, 1000);
} else if (mode === "stderr_flood") {
  setInterval(() => process.stderr.write("x".repeat(65536)), 5);
  setInterval(()=>{}, 1000);
} else if (mode === "pid_pressure") {
  const kids = [];
  const spawn = () => { try { kids.push(require("child_process").spawn("node",["-e","setInterval(()=>{},1000)"])); } catch {} };
  const timer = setInterval(spawn, 50);
  setInterval(()=>{}, 1000);
} else {
  setInterval(()=>{}, 1000);
}
