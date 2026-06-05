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




we should not try to build this all in one go. lets start by building the SVG representation of the geometry in geom.yaml.  make a module for this with tests. use the tests/ouput folder to store the restuling svg for each example. since our integration stack is getting longer, lets refactor the tests so that we have one folder per test. inside of tests/dxf_clean, make a new folder for each example ( right now, three-- '2xintake', 'intakev4', and 'intake_frontv2' ). then, use those same folders under output, so that the outputs are grouped by input case.  since these tests also use the content in machine.yaml, lets prepare for richer, independent tests by putting a copy of machine.yaml as it exists now into each test-- the test should use that machine.yaml, not the current one . 

(4) use the combination of machine.yaml and planner.yaml to generate an operation plan. if there are any questions remaining, ask the user
(5) show a visual of the job, and interact with the operator. this phase includes gathering things we need that are not provided in the job template. In this phase the user can edit the op.yaml file directly

(6) user can download the buntle, which has all the original dxf, fixed dxf, geom.yaml, and op.yaml, plus the code. 


ok lets build a simple ui for now that goes as far as generating geom.yaml, but no further. we just need to stub out the ui elements for hte later bits. here's what we want this to look like:

(0) the ui consists of a display area, a chat scroll text box, and a menu bar. for now, use my local machine.yaml and planner.yml, but later we'll let the user upload/maintain their own. the menu bar has settings on the far right. from the left, we have progress moving left to right. initially, there is a button that reads 'choose a dxf'.  later steps appear between markers like xya.dxf > next step > next step.  prioer steps are linkable-- clicking hte link goes back to that step
(1) the initial view shows a 2-d view of our machine work area, and the details of our macine ( uom, size, preferred z stock position). 
(2) user chooses a dx file
(3) fix and generate geom.yaml. then display the fixed geomtry.  show part boundaries bolder, with outer bondaries in darker blue, and inner holes in lighter blue. show node markers between segments. that way, the user can easily see if we have separate entities or not. in practice this will help me validate our work so far. when you hover over an entity, show its type, number of nodes, etc.  If there is a frame, show it as green.

its important to design the ui realizing that hte user could start with a dxf, but in the future they could start with a dxf and a geom.yaml file (later meaning, whe nother tools can do the geometry recognition ).

create me a small run.cmd that will launch the nicegui app-- and you should use this too so i know i'm running what you are running. 

before you write this, ask me any clarifying questions.




ok i had a look at this version.  (1) lets not show the job on the full workspace on the planning screen-- lets just show the job itself based on geom. we'll place the job into the machine in the next step. (2) in the chat, i had asked earlier to show all of our advice, but lets not do that, it takes too much space. add menu bar items that allow showing planner.yaml and machine.yaml. you can open those in a popup with a button to dismiss.  (3) in the chat make it more clear which planning in puts are required and what you have.  To do that, make a more clear set of input: value pairs. present them as cards or some other clever layout with three values: in bold: what the thing is, then, where you got it, and an editable place to override.   for example, in my case, you got stock details from machine.yaml.  These come from the operation_inputs.yaml file.

this editable area probably needs three lines to fit in the right pane.
when editing, use drop down lists for things with constrained values. that includes the stock thickness unit of measure, the materials list ( for now, plywood, polycarbonate), the tool or tools, the workholding method, stock origin, and coordinate system.  you an use collabsable elements if needed.  the generate plan button should be disabled if you dont have all the inputs you need.

(4) we need a separate pane to summarize what we found in the geometry. the information in the summary is fine, but use two-colume item: value format, and make it look nice. use a collapseable pane above the area for inputs.  that one can be collapsible too.  so we'll have four sections in that right pane: geometry summary, operation inputs, text entry, and generate plan button.  


ok time for the exciting part-- the planning step!

almost all of the advice needed can be mentioned in the operation_advice area. i've added a new section called strategies.

I've now realized that it important to show the entity names in the geometry output.  Show these with a green font, large enough to see easily ( larger than other items), with a leader pointing to entity.  this is crucial, because that way, i can simply type "e2 should be a pocket x deep".  

i realized we need to add clear_z to the machine.yaml 

for tools, we need a new key flute_spiral, with choices straight, downcut, upcut, and compressions cut.  change type to end_type, choices flat, ball, o-flute

Lets add a new file called system_planner_advice.yaml, that's inside the source tree. this advice should be used together with the user provided planner.yaml. It contains things that the planner should handle, but that an experienced user would assume are already checked.  Many of these things will result in planning errors we'll have the user handle on the next page. These are not missing inputs: they are problems that have been found based on the provided inputs.  These should be categorized using the same keys as in the planner.yaml.  The keys are veyr useful for the ai to understand what each one's advice is for. Here are a few entries to get us started:

workholding:
   - if using tabs, the tabs should be 0.06" tall for polycarbonate, 0.1" tall for wood or plywood
   - tabs should be placed on linear segments. if enough locations are not available, warn the user
   - tab width should be 1x material thickness
   - if tabs are used, try to get 4 tabs per part, plus 1 tab every 10x the thickness of material. of length if four was not enough.  if there are insiffucieint places for tabs on straight locations, warn the user  
   - place tabs opposite each other as possible.
   - if using screws, screw locations should be placed in areas where there will be scrap.
   - pre-drill screw locations using the same bit as will be used for the job, if the job is a one tool job. 
   - if not a one tool job, pre-trill the screw locations with the smallest tool available
   - screw locations should be placed somewhere about 1 two square feet.
   - always put screws in the four corners of the stock, if using screw workholding.

strategies:
   - always start by inspecting the geometry, and find the largest tool diameter that will do the job.  
   - only use 1/8 inch tools if necessary to meet criteria
   - always generate transition ramps into the cut
   - always plan the job in this order: holes for screws, interior pockets, interior holes, outer boundaries. 
   - use feedrates associated with the tools, unless specified otherwise
   - use climb cutting for finishing passes use conventional cutting for roughing passes that are 
   - always use climb cutting on plastics and compression cutters.
   - for aluminum, use conventional for all passes except finishing passes.

put all those into system_planner_advice.yaml. Then, go do some cnc router research, and add additional strategies, workholding, and tool advice that is important for cnc routers.  Then, i'll ajudst later.

build a python service that accepts a geom.yaml, system_planning_advice object, user planning advice, user inputs, and generates an operation plan object.  This service interface is crucial because it will also be a standalone web service. make sure to design a response object that ahndles errors and warnings, in addition to sending the proposed plan.  the response should always be the same structure-- errors, warnings, and a place for a plan object--though it is permissible for the plan to be empty.  package this esrvice in a fastapi endpoint, and make sure that the fastapi endpoint is used from the ui.  I want the ui to use the same endpoint as is available outside the ui.

its very important that this endpoint is stateless-- we dont want it reading the local filesystem. This may mean we need to include the machine object as input as well, but i'm not sure.  



In the ui, as this first step, just show the output op.yaml source till we get it tweaked in.

with that done, i think you should be able to make a plan. use examples/job.yaml as an example. 
  

ok this is looking really good! changes:

changes to settings:
(1) see picture 1: the machine yaml is partially cut off ( the planner looks fine) 
(2) in settings, lets avoid the double-scrolling. instead of having both texts having their own scroll, just let them be full length and use the main window scroll.

operation planning:
(1) when completing a section that's missing data, it should go to green once the data is supplied ( so i know what i have is acceptable).  give a green check icon and red x icon in the title bar to make it clear. ( all sections that are ok should be green checkmarks).
(2) add a visual indicator ( green checkmark, red x icon) to the create a plan button whenever it can be clicked. 
(3) add a toolbar above the graphics area but below the main menu. put the zoom to extents button in that bar. also put buttons to rotate the geometry 90 dgrees left and right ( use the conventional rotate triagle icons for that).  make sure to re-generate all the labels when that happens so they are readable!
(4) add zoom in and zoom out buttons in that toolbar
(5) add button to show /hide entity labels and dimension labels ( start with these layers enabled. these buttons should be the type that appear depressed and undepressed when toggled
(6) the zoom extents button still doesn't do anything, i think it could be due to this exception but im not sure: 

The parent element this slot belongs to has been deleted.
Traceback (most recent call last):
  File "C:\Users\davec\gitwork\dxfwiz\.venv\Lib\site-packages\nicegui\events.py", line 480, in _await_and_handle_in_context
    await awaitable
  File "C:\Users\davec\gitwork\dxfwiz\src\dxfwiz\ui\app.py", line 660, in handle_upload
    ui.notify("geom.yaml generated", type="positive")
    ~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\davec\gitwork\dxfwiz\.venv\Lib\site-packages\nicegui\functions\notify.py", line 52, in notify
    client = context.client
             ^^^^^^^^^^^^^^
  File "C:\Users\davec\gitwork\dxfwiz\.venv\Lib\site-packages\nicegui\context.py", line 41, in client
    return self.slot.parent.client
           ^^^^^^^^^^^^^^^^
  File "C:\Users\davec\gitwork\dxfwiz\.venv\Lib\site-packages\nicegui\slot.py", line 31, in parent
    raise RuntimeError('The parent element this slot belongs to has been deleted.')
RuntimeError: The parent element this slot belongs to has been deleted.

During handling of the above exception, another exception occurred:

Traceback (most recent call last):
  File "C:\Users\davec\gitwork\dxfwiz\.venv\Lib\site-packages\nicegui\background_tasks.py", line 152, in _handle_exceptions
    task.result()
    ~~~~~~~~~~~^^
  File "C:\Users\davec\gitwork\dxfwiz\.venv\Lib\site-packages\nicegui\events.py", line 482, in _await_and_handle_in_context
    core.app.handle_exception(e)
    ~~~~~~~~~~~~~~~~~~~~~~~~~^^^
  File "C:\Users\davec\gitwork\dxfwiz\.venv\Lib\site-packages\nicegui\app\app.py", line 176, in handle_exception
    if context.slot_stack and context.client is not None:
                              ^^^^^^^^^^^^^^
  File "C:\Users\davec\gitwork\dxfwiz\.venv\Lib\site-packages\nicegui\context.py", line 41, in client
    return self.slot.parent.client
           ^^^^^^^^^^^^^^^^
  File "C:\Users\davec\gitwork\dxfwiz\.venv\Lib\site-packages\nicegui\slot.py", line 31, in parent
    raise RuntimeError('The parent element this slot belongs to has been deleted.')
RuntimeError: The parent element this slot belongs to has been deleted.


I'm getting errors like these when loading intake4. should i worry?
Found non-unique entity handle #90, data validation is required.
Found non-unique entity handle #90, data validation is required.
Found non-unique entity handle #90, data validation is required.
Found non-unique entity handle #93, data validation is required.
Found non-unique entity handle #96, data validation is required.
Found non-unique entity handle #9a, data validation is required.
Found non-unique entity handle #9e, data validation is required.
Found non-unique entity handle #9e, data validation is required.
Found non-unique entity handle #9e, data validation is required.
Found non-unique entity handle #a5, data validation is required.
Found non-unique entity handle #a5, data validation is required.
Found non-unique entity handle #a5, data validation is required.
Found non-unique entity handle #a5, data validation is required.
Found non-unique entity handle #ae, data validation is required.
Found non-unique entity handle #ae, data validation is required.
Found non-unique entity handle #ae, data validation is required.
Found non-unique entity handle #ae, data validation is required.
Found non-unique entity handle #ae, data validation is required.
Found non-unique entity handle #ae, data validation is required.
Found non-unique entity handle #ae, data validation is required.
Found non-unique entity handle #ae, data validation is required.
Found non-unique entity handle #ae, data validation is required.
Found non-unique entity handle #ae, data validation is required.
Found non-unique entity handle #ae, data validation is required.
Found non-unique entity handle #ae, data validation is required.
Found non-unique entity handle #ae, data validation is required.
Found non-unique entity handle #ae, data validation is required.
Found non-unique entity handle #ae, data validation is required.
Found non-unique entity handle #ae, data validation is required.
Found non-unique entity handle #ae, data validation is required.
Found non-unique entity handle #ae, data validation is required.
Found non-unique entity handle #b8, data validation is required.
Found non-unique entity handle #b8, data validation is required.
Found non-unique entity handle #b8, data validation is required.
Found non-unique entity handle #b8, data validation is required.
Found non-unique entity handle #bf, data validation is required.
Found non-unique entity handle #bf, data validation is required.
Found non-unique entity handle #bf, data validation is required.
Found non-unique entity handle #bf, data validation is required.
Found non-unique entity handle #bf, data validation is required.
Found non-unique entity handle #bf, data validation is required.
Found non-unique entity handle #cf, data validation is required.
Found non-unique entity handle #cf, data validation is required.
Found non-unique entity handle #cf, data validation is required.
Found non-unique entity handle #cf, data validation is required.
Found non-unique entity handle #cf, data validation is required.
Found non-unique entity handle #cf, data validation is required.
Found non-unique entity handle #df, data validation is required.
Found non-unique entity handle #e4, data validation is required.
Found non-unique entity handle #e9, data validation is required.
Found non-unique entity handle #ee, data validation is required.
Found non-unique entity handle #f3, data validation is required.
Found non-unique entity handle #f8, data validation is required.
Found non-unique entity handle #fd, data validation is required.
Found non-unique entity handle #102, data validation is required.
Found non-unique entity handle #107, data validation is required.
Found non-unique entity handle #107, data validation is required.
Found non-unique entity handle #107, data validation is required.
Found non-unique entity handle #107, data validation is required.
Found non-unique entity handle #107, data validation is required.
Found non-unique entity handle #107, data validation is required.
Found non-unique entity handle #107, data validation is required.
Found non-unique entity handle #107, data validation is required.
Found non-unique entity handle #107, data validation is required.
Found non-unique entity handle #107, data validation is required.
Found non-unique entity handle #107, data validation is required.
Found non-unique entity handle #107, data validation is required.
Found non-unique entity handle #107, data validation is required.
Found non-unique entity handle #107, data validation is required.
Found non-unique entity handle #107, data validation is required.
Found non-unique entity handle #107, data validation is required.
Found non-unique entity handle #107, data validation is required.
Found non-unique entity handle #107, data validation is required.
Found non-unique entity handle #107, data validation is required.
Found non-unique entity handle #107, data validation is required.

(7) generate plan puts the plan into the tiny sidbar! that's not right!
We should move to another screen -- the one labelled 'toolpaths'.  
on this screen we should repeat the geometry on the right, but using only about 2/3 of the screen. on the right, we should have the yaml from the operation plan.  eventually we'll change the display here to show the operations, but for now we're just repeating the geometry.  
also on this new screen you can stub out the piecres for the next step-- a post processor selection ( right now the only choice is uccnc, but use a drop down box for later).  and an action button that says generate toolpaths. and below that a disabled button for download gcode/bundle. but disabled ( it will get enabled when toolpaths are generated. 
