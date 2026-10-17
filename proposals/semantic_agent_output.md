# Semantic Classification Agent run

Generated 2026-10-17 12:41 UTC. 99 distinct values classified in 1 request(s). **Applied** to `config/category_mapping_agent.json`; silver uses it on the next run.

- Coverage: 100.0% of input values; 0 missing, 0 invented keys and 0 off-taxonomy labels dropped.
- Mapped: 46; swapped-column values: 46; unmapped junk: 7.
- Agreement with hand-built baseline: 46/46 spellings (100.0%).
- Swapped values matching the ticket's own label: 46/46 (100.0%).

## Mapping

| raw value | canonical |
|---|---|
| cleaning | Cleaning |
| housekeeping | Cleaning |
| janitorial | Cleaning |
| elec | Electrical |
| electrical | Electrical |
| electrical systems | Electrical |
| power | Electrical |
| power issue | Electrical |
| elevator | Elevator |
| elevator/escalator | Elevator |
| lift | Elevator |
| vertical transport | Elevator |
| fire alarm | Fire Safety |
| fire safety | Fire Safety |
| fire/safety | Fire Safety |
| sprinkler | Fire Safety |
| general | General Maintenance |
| general maintenance | General Maintenance |
| maintenance | General Maintenance |
| a/c | HVAC |
| ac | HVAC |
| air conditioning | HVAC |
| climate control | HVAC |
| heating cooling | HVAC |
| heating/cooling | HVAC |
| hvac | HVAC |
| hvac system | HVAC |
| connectivity | IT & Network |
| it | IT & Network |
| it support | IT & Network |
| it/network | IT & Network |
| network | IT & Network |
| wifi | IT & Network |
| misc | Other |
| other | Other |
| exterminator | Pest Control |
| pest | Pest Control |
| pest control | Pest Control |
| plumbing | Plumbing |
| plumbing issue | Plumbing |
| water issue | Plumbing |
| water/plumbing | Plumbing |
| access control | Security & Access |
| badge/access | Security & Access |
| security | Security & Access |
| security systems | Security & Access |

## Swapped (category held a sentence)

- bird droppings on parking garage entrance canopy. looks unpr -> Cleaning
- breaker keeps tripping in server room 696. critical — affect -> Electrical
- bugs in office remote site beta -> Pest Control
- cold -> HVAC
- elevator a-001 stuck between floors 4 and 12. no one inside -> Elevator
- elevator buttons for floor 1 not responding. hq floor 1. -> Elevator
- elevator e-048 stuck between floors 2 and 9. no one inside ( -> Elevator
- elevator making grinding noise. annex east main elevator. -> Elevator
- elevator making grinding noise. annex west main elevator. -> Elevator
- elevator out of order again parking garage -> Elevator
- emergency exit sign burnt out, hq floor 2 6 floor east stair -> Fire Safety
- emergency exit sign burnt out, main office 8 floor east stai -> Fire Safety
- faucet in remote site alpha kitchen won't turn off completel -> Plumbing
- fire extinguisher expired in hallway near room 470, tower b. -> Fire Safety
- furniture request — need 3 additional chairs for room 778. -> General Maintenance
- hot water not working in annex east. multiple sinks affected -> Plumbing
- hot water not working in building 7. multiple sinks affected -> Plumbing
- hot water not working in cafeteria block. multiple sinks aff -> Plumbing
- hot water not working in tower b. multiple sinks affected. -> Plumbing
- need access card programmed for new employee starting monday -> Security & Access
- need more outlets in conf room 387 -> Electrical
- need more outlets in conf room 861 -> Electrical
- network jack at desk 301 not active. new hire needs connecti -> IT & Network
- pipe burst -> Plumbing
- restrooms on 2 floor haven't been serviced today. -> Cleaning
- restrooms on 7 floor haven't been serviced today. -> Cleaning
- security camera a-040 offline since yesterday. covers parkin -> Security & Access
- security camera e-049 offline since yesterday. covers parkin -> Security & Access
- smoke detector beeping tower a -> Fire Safety
- temperature control issue in conference room 379. the thermo -> HVAC
- temperature control issue in conference room 618. the thermo -> HVAC
- the ac unit on 6 floor is not working. room temp is around 5 -> HVAC
- toilet in 7 floor men's restroom is overflowing. urgent. -> Plumbing
- trash overflowing in kitchen area annex west -> Cleaning
- trash overflowing in kitchen area cafeteria block -> Cleaning
- trash overflowing in kitchen area hq floor 1 -> Cleaning
- unit c-050 cycling on and off every few minutes. coils might -> HVAC
- unit h-008 cycling on and off every few minutes. coils might -> HVAC
- vpn issues from hq floor 1 conference room. can't connect to -> IT & Network
- vpn issues from remote site alpha conference room. can't con -> IT & Network
- vpn issues from warehouse north conference room. can't conne -> IT & Network
- wasp nest forming on hq floor 3 exterior, near entrance. -> Pest Control
- water leak detected in ceiling tiles, building 7 room 805. a -> Plumbing
- wheelchair-accessible elevator in annex east has expired ins -> Elevator
- wifi down on 4 floor, building 7. multiple users affected. -> IT & Network
- window cracked hq floor 2 6 floor -> Other

## Unmapped

- `unknown`
- ``
- `delete me`
- `asdf`
- `null`
- `test`
- `???`
