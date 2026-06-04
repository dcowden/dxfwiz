ok now its time to do some work towards generating an operation plan.

ok before we get ready to work on operation plans, we need to do some work determinging drawing units.  if the dxf includes specific units, we should use those units. but if it does not, we should make an educated guess.

I would imagine i'm not the first person to come up with the need to make a heuristic approach for guessing mm vs inches ( those are the only ones we should consider).  what heuristics do people tend to use? the ones I use are (a) for a cnc router, no feature is smaller than 0.01 inches, and nothing is going to be bigger than 9 feet. also, if there are holes, we can guess that users typically use common sizes.  if a hole works out to be a common mm size, or a common fractional size, we can guess that way.  

what other heuristics are used? 


 i'd also like to promote the frames stuff we did into two places.  first, in job template we should add an auto section that allows selecting frames and recognizing them as stock vs not.  this can be added to the auto rules. that way, we can eaisly disable this logic. then, when we recognize, we need to reflect our findings iin geom.yml. if frames are enabled, then we should add a stock section, and treat the outer frame as the stock-- we do this ONLY if there is there is one rectangular frame, and it contains items, but no other rectangular frames do.  if we detect stock frame, we should add a stock section, and note which dxf handles this one is-- then it should be removed from the entities tree. this way, the entities will refelct actual parts, not parts and holes. 


ok now its time to do some work towards generating an operation plan.
first thing is a small change: for machine.yaml, i want to add a field to allow specifying whether the machine has a tool changer or not. we can do this with a maximum tools parameter: 1 means no changer, > 1 means we have a tool changer with that many tools. 

now with that added, we can talk about generating an operation plan.  

the overall flow i want to use is:
(1) user chooses a dx file
(2) fix and generate geom.yaml
(3) use the combination of machine.yaml and job_template.yaml to generate an operation plan.
(4) show a visual of the job, and interact with the operator. this phase includes gathering things we need that are not provided in the job template. In this phase the user can edit the op.yaml file directly

(5) user generates gcode from the op.yaml by clicking a button.
(6) user can download the buntle, which has all the original dxf, fixed dxf, geom.yaml, and op.yaml, plus the code. 

before we work on the flow, though, we need some changes to job_template.yaml.  
The goal is to create content whereby we can send the job_template.yaml, the fixed dxf, and geom.yaml to an ai, and get a reasonable op.yaml plan as output

(1)  we need a default value for max_tools.  this sets the number of tools i'm willing to use at most, it should be optional. note that max tools is different than the one in machine-- if the machine has 1 tool, but max_tools is 2, it means i'm willing to tolerate one tool change.

(2) operation plan guidance prompts.  we want several blocks of English text ( format as a list of items, not one big paragraph. we'll nest this under a single key, operation_advice, with these entries under it ( each entry should support a list of English instructions):
   (2a) workholding. advice on which type of work holding to use. example: i have a presser foot, so i want to use screws in the corners, or "i have a spoilboard, so i want to use screws"
   (2b) tools. advice on how to use tools.  exampels: i want to use only one tool for the whole job, so choose the largest tool that can use all the features
   (2c) geometry. how to interpret entities. our framing instructions i gave you, for example, "if there is a rectangle enclosing all parts, then assume that's the stock. otherwise, ask me for the stock size

(3) required inputs to generate a plan.  There is certain data we need to get from the user one way or another. This list is not user-modifyable:
   - stock size
   - stock material
   - workholding method
   - z_zero position
   - coorindate system ( G54, G55 etc).
The planner should use the machine config, and job_template to answer as many of these questions as possible-- but if we cant, then we must ask the user to provide them.  I'd like to create a new file called operation_inputs.yaml, that configures this list. The operation planner should read this file to understand what it must get before attempting to create a plan. 


now i think we're far enough to build the GUI element that we might use to interact with the user. the requirement is to build this as a nicegui application, so i can serve via web. all services used by the app should be fastapi services.  
The first decision we have is how to render the dxf entities.  i am thinking svg and svg.js, because then we can include an svg in the bundle. But are there other alternatives we should consider? we should plan for the fact that the graphical display must allow showing the original geometry, the propose operations, and the proposed tool paths. before we commit to anything, lets realize that we face the problem of generating gcode and showing those tool paths. what tools can we use to do that? its not trivial at all, especially for pocketing-- so it makes sense to ask what visualization tools exist for gcode and what geometry-> gcode tools exist, before we build the UI. 