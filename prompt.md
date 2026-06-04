# dxfwiz

This project makes it very fast to cut part on a cnc router from a 2d dxf or svg file.

The key differentiator is the use of structured, plain text yaml files for each step, so that agents can easily help out.

## overall flow
As a one time set up step, the user creates several yaml files that detail his machine, tools, and cnc post processor.

To process a job, the user starts with a 2d vector file ( dxf, svg). The software will perform these steps:
   1. Clean the file, and produce a yaml file describing the geometry that was recongized
   2. as the operator for intent, indicating what operations should be performed. The user is presented with a preview
   3. create a job.yaml file, presenting the operations that hsould be performed. this is done by using the provided intent, and machine files
   4. generate gcode for the user's machine, using provided post processor details.
   5. transmit the file to a cloud service so it can be pulled to the machine ( We shoudl assume that the person doing steps 1-3 is NOT the same as the machine operator)

The key element of the design of this software, differentiating it from others, is that open source yaml files are created at each step, allowing different tools to be used.  The most important of these files is the operation/job file, which conveys the jobs to be perfomed.  The fact that this file is open format means agents can easily generate it.  

## file specifics
yaml files contain the information needed to move from raw 2.5 d graphic vector file ( dxf, svg ) to gcode that can be executed on a cnc router. those files are:

    1. machine.yaml.  machine config. includes machine dimensions, coorindate system, tool library, fixturing methods, this stuff is the physical 
    etc
    2. cnc_post.yaml. cnc machine dialect, ( in my case, uccnc). configures how gcode will be generated

    3. planner.yaml. planner guidance file for jobs. this contains defaults and English operation advice used to create new operation plans, so that we can reduce what's needed in each job file

    4. geom.yaml. a file produced from reading a vector file, and reducing it to recognized entities.  One software stage is responsible for reading the raw dxf/svg, and then creating this.  This file has summary imformation ( number of entities, bounding box, etc). Most importantly, we have a tree of entities, oranized with outer entities at the top, and child entities nested undereath their enclosing loops. each entity can be closed or open.  we also have properties for regognized shapes ( hole, line)

    4. job.yaml. a file that contains the details for a job we will run. this includes the operations we will perform, using all the typical 2.5 d operations: drill, helical drill, pocket, contour.  typical options include climb/conventional mill direction, stock origin, leave/finishing distance, stock thickness, and all the usual options.

## Key project considerations
1. The power of this project is the open yaml files. The intent is to build an ecosystem that disrupts the market. Its important that these strike the right balance of fleexibilty and simplicty.  We're generally targeting 2.5 d routers and lasers, NOT 5 axis machines. 

2. Design for multi-user flows. One common caes, of course will be one user perofrming all steps.  But the flow I'm going to use first is FRC teams. In this case, the machine operator is separate from the people generating the job files.  The flow in that case is liek this:
    2a. Machine operator creates tool, post processor, and machine files, and provides them in a shared location of some sort
    2b. a student begins with a cad pacakge or dxf, and uses the software to create a job file and geometry file, along with a cleaned vedtor file. These files are stored in a shared location, and zipped are called a 'bundle'
    2c. the operator has the post processor, and generates gcode from the bundle, which can be executed

    It is possible that the student might also generate the gcode as well. 

3. my initial case is frc students beginning with solidworks or onshape.  one very likely total solution is an onshape app, which hosts various machines and their associated files.  Students can skip the step of saving a dxf and possibly some of the step to provide intent, if they have a 3d file. 

4. no matter what, all users what a confirmation of what will happen. this will typically inculde a simulation/preview. This should be possible from the operation file, and should not require gcode.  Users of plugins ( like solidworks/onshape) might be able to get his in the cad software itself, but we will likely ahve to build one ourselves too

5. this tool must be browser based, with nothing to install locally. files should also be shared that way. I'll probably be hosting on render.com

6. yaml files provide flexible points and unlock agents!

7. even though i am targeting a huge discruption, for now i want to start small and solve my particular problem, which is below

## My current broken flow [ reference ]
I'm doing this work today, and it illustrates the pain. my steps are:

1. take a dxf from a student. this is a shitty solidworks export that has tiny line segments that dont touch each other and many other problems requiring it to be cleened.
2. import into lightburn.  
3. auto-join. this fixes lots of the issue
4. create loops/groups as needed
5. export a dxf that doesnt suck
6. import that dxf into estlcam.
7. use estlcam to generate gcode.  Estlcam has several things that work well:
   7a. UCCNC post
   7b. settings that allow auto mapping holes in a given range as drills, and in another range as helical drills
   7c. recognizes controus automatically as holes or bosses, based on their order from the outside. a 'is there an outer frame' option allows providing an outer frame that's the stock size, which is nice. 
   7d. an 'has outer frame' option ignores that outer frame for purposes of identifying bosses vs holes
8. save gcode.
9. open gcode on uccnc and run the program

## The workflow i want
After creating machine configs and such, i want to reduce the broken flow to these steps:

   * [one time] create machine, post, and tool config files. take dxf/svg from student

Then, for each job

   1. load dxf|svg file. this fixes the vector file, generates geom.yaml, and presents an interface to user showing the entities found, machine boundaries from amchine.yaml.  Very importantly, each loop/entity is labeled with an id, and possibly other things like circle/closed/open, etc.
   2. prompt user via regular text chat to describe their machining intent in english, like "this is a bearing pillow block. put the origin at the bottom left, machine the center with a parallel stepover pocket, and a full depth finishing pass., etc
   3. click generate plan. this should show a preview of what will happen, plus generate the job.yaml. the user should be able to manually edit this yaml ( this is a key benefit. I HATE it when you have to re-click everything just to provide a few small changes)
   4. save bundle. this is a zip file containing the cleaned vector file, the geom.yaml, and the job.yaml.  This is what the machine operator will use to generate gcode.

   Steps 2-6 in my broken curent flow happen in 1 step in this new workflow. gathering all intent is just one more step

## software tools/libarires

* this is a professional project-- so we need unit test coverage and modular components
* python 3.x, pytest
* AI for intent: litellm, instructor
* cloud provider: render.com
* python project: packaged as editable package, using pyproject.toml
* project build tool: uv
* libraries for geometry processing: shapely, ezdxf, pyclipper,numpy, scipy
* for web: nicegui, fastapi, 
* for file editing: jsonschmea, ruamel
* ai: prefer gemini but support others
* building gcode: pygcode maybe
* buidl a nicegui app-- i want a python code base with minial other libraries
* very important to unit test the code representing the geometry, and the code doing processing
* ai's role shoud be limited in assisting humans assigning INTENT, which means the step going from a geometry yaml file to an operation file. 

* use existing uccnc post processors for inspriation: there's a fusion 360 UCCNC post, which can be ported from javascrpit

## software architecture
this code will get complex, so the module organizatin is important. create separate modeules with both unit tests and integration tests as follows:

* cleaning dxfs. this should use ezdxf. unit tests should include simple cases for gap tolerances, loop formation, and duplicate detection. integration tests should be intentionally messy dxfs with known right answers

* organizing loops. this should use shapely. unit and integration tests should include loops and non loops, nested loops up 5o 4 deep.

* reading/writing each yml file. each file should have a pydantic object for it. unit tests should validate several actual yml files. 

* a post processor defintion and associated yml file

* gcode generator reading a cnc post defintion and an operation plan.

* service objects for each workflow step: input dxf -> clean -> organzie -> geometry.yml for examples

* services for creating operation plans  from geometry, machine configs, and human input, by passing human english into an ai prompt

* service for displkaying a simulation of an operation plan\
* fast api points for each of the service above

* a gui using nicegui

## file architecture
tests should go in a tests/folder. data for tests goes in tests/data.  pyproject.toml in root. project source in a package, development done as editable package.

## inital scope
initially i only care about uccnc, 2.5d routers, and the workflow described above.  I want to get a working prototype running as quickly as possible.  Though we are desiging for tsudents and multi-uesr flow, i'll be running it all myself locally for now, so we do not care about we scalabilty and deployment

# sprint 1-- generate base project skeleton, and generate example yaml files for review

# clarifications:
answers: question 1: operator intent. the input should be plan text from the user, plus a text editor to allow th euser to directly edit the yaml files.  budnles: yes, include the original file as _original.  and the fixed one as _fixed.  ai is in scope for hte first scope, but not the first sprint, which is focused on yaml files.   yaml files: machine.yaml should be only machine physical things: work envelope, coorindates, tools.  the planner contains things that are decisions the operator makes, but are NOT physical. examples would be default tool, default milling type ( climb/confentional), default stock thickness, default coordinate system ( g55), and English operation advice. for geom.yaml yes the tree is by containment. loops containing the same objec,t they should be siblings, yes.  for posts, yes this should include all the usual dialect differences necessary. you can find out what's typically required by looking on fusion360 posts, which are javascript typically.  the one for UCCNC is a good start

# my machine details
er11 spinele
vertically oriented, 3 axis mill
typical tools: 1/8 flat 2 flute end mill, 1/8" 1 flute end mill, 3/16 2 flute compression endmull, 3/16 2 flute upcut endmill, 1/4" 2 flute upcut, 1/4" 1 flute upcut
x axis is towards the right, y is up, z is toward operator. origin in bottom left

# sprint 1 schema review feedback
1. units: each yaml file that has units should specify preferred units for length and speed at the file level. I use inches and inches/minute.
2. tools: should have default speed (rpm) and feedrate (ipm or mm/s), plus plunge rate and depth_per_pass. these are the defaults unless overridden in the operation. flute_length and total_length should be optional-- they rarely matter for 2.5d.
3. coordinate system: do not specify z direction -- derive it from x and y. use 'right', 'left', 'up', 'down' (not 'away from operator'). my machine: x=right, y=up.
4. work envelope: remove units from work_envelope since units are set at the file level.
5. spindle max_rpm belongs in machine.yaml, NOT in cnc_post.yaml. 
6. cnc_post.yaml also needs file-level units.
7. planner.yaml: remove the entire operation_defaults section. move feed_rate, plunge_rate, and depth_per_pass onto tools in machine.yaml instead.  Keep defaults and English operation_advice sections; do not use structured auto_rules.
8. geom.yaml entity-to-dxf linking: entity IDs in geom.yaml must be traceable back to entities in the cleaned dxf. approach TBD.
9. geom.yaml frame detection should be strict. A frame is only a rectangular closed loop with exactly four straight sides, usually representing stock size. If multiple frames are present and only one contains profiles, recognize only the populated rectangle as the frame in the containment tree and ignore the empty stock rectangles for semantic part nesting. If all top-level closed loops are rectangles and none contains profiles, treat them as parts, not frames.
10. geom.yaml unit handling: if the DXF declares units, use those units. If the DXF is unitless, guess only between inches and millimeters using documented evidence: router-scale plausibility, minimum feature size, common circular hole sizes, common stock/frame sizes, and weak DXF metric/imperial hints such as $MEASUREMENT. geom.yaml should include the selected length unit, whether it was explicit or guessed, confidence, and evidence for the decision.
