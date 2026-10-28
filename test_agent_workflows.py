import unittest
import agent_workflows

class WorkflowTests(unittest.TestCase):
    def test_gmail(self): self.assertEqual(agent_workflows.choose_workflow('gmail account banana'), 'gmail')
    def test_channel(self): self.assertEqual(agent_workflows.choose_workflow('create a YouTube channel'), 'youtube')
    def test_video(self): self.assertEqual(agent_workflows.choose_workflow('make a video and upload'), 'video')
    def test_invest(self): self.assertEqual(agent_workflows.choose_workflow('invest in stock market'), 'invest')
    def test_research_default(self): self.assertEqual(agent_workflows.choose_workflow('research a new topic'), 'research')
    def test_plan_boundary(self): self.assertIn('PLAN ONLY', agent_workflows.render('invest', 'invest'))

if __name__ == '__main__': unittest.main()
