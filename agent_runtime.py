
class AgentRuntime:
    def __init__(self, max_steps=10, max_retries=2):
        self.max_steps = max(1, min(max_steps, 20))
        self.max_retries = max(0, min(max_retries, 3))
        self.running = False

    def run(self, steps, execute_fn, verify_fn):
        if self.running:
            return {"success": False, "error": "Agent is busy"}

        if not isinstance(steps, list) or not steps:
            return {"success": False, "error": "No steps provided"}

        if len(steps) > self.max_steps:
            return {"success": False, "error": "Too many steps"}

        self.running = True
        report = []

        try:
            for number, step in enumerate(steps, start=1):
                success = False
                error = None
                attempts = 0

                for attempt in range(self.max_retries + 1):
                    attempts += 1
                    try:
                        result = execute_fn(step)
                        if verify_fn(step, result) is True:
                            success = True
                            break
                        error = "Verification failed"
                    except Exception as exc:
                        error = str(exc)

                report.append({
                    "step": number,
                    "action": step,
                    "success": success,
                    "attempts": attempts,
                    "error": error,
                })

                if not success:
                    return {
                        "success": False,
                        "report": report,
                    }

            return {"success": True, "report": report}

        finally:
            self.running = False
if __name__ == "__main__":
    agent = AgentRuntime()

    result = agent.run(
        steps=["test step"],
        execute_fn=lambda step: "done",
        verify_fn=lambda step, result: result == "done"
    )

    print(result)