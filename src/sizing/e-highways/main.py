import pypsa
from os.path import join, dirname, abspath, isdir
from os import makedirs
import yaml
from time import strftime
import pandas as pd
import numpy as np

from src.data.emission import get_reference_emission_levels_for_region
from src.data.load import get_load_from_nuts_codes
from src.data.topologies.ehighways import get_topology
from src.network_builder.res import add_generators_from_file as add_res_from_file
from src.network_builder.res import \
    add_generators_using_siting as add_res, \
    add_generators_at_resolution as add_res_at_resolution, \
    add_generators_per_bus as add_res_per_bus
# from src.network_builder.res import add_generators_at_bus_test
from src.network_builder.nuclear import add_generators as add_nuclear
from src.network_builder.hydro import add_phs_plants, add_ror_plants, add_sto_plants
from src.network_builder.conventional import add_generators as add_conventional
from src.network_builder.battery import add_batteries
from src.data.geographics import get_subregions
from src.postprocessing.sizing_results import SizingResults

import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(asctime)s - %(message)s")
logger = logging.getLogger()

NHoursPerYear = 8760.

if __name__ == "__main__":

    # Main directories
    data_dir = join(dirname(abspath(__file__)), "../../../data/")
    params_dir = join(dirname(abspath(__file__)), "../../../data/technologies/")
    output_dir = join(dirname(abspath(__file__)), f"../../../output/sizing/e-highways/{strftime('%Y%m%d_%H%M%S')}/")

    # Run config
    config_fn = join(dirname(abspath(__file__)), 'config.yaml')
    config = yaml.load(open(config_fn, 'r'), Loader=yaml.FullLoader)

    # Parameters
    tech_info = pd.read_excel(join(params_dir, 'tech_info.xlsx'), sheet_name='values', index_col=0)
    fuel_info = pd.read_excel(join(params_dir, 'fuel_info.xlsx'), sheet_name='values', index_col=0)
    pv_wind_tech_config = yaml.load(open(join(params_dir, 'pv_wind_tech_configs.yml')), Loader=yaml.FullLoader)

    # E-highway clusters information
    eh_clusters_file_name = join(data_dir, "topologies/e-highways/source/clusters_2016.csv")
    eh_clusters = pd.read_csv(eh_clusters_file_name, delimiter=";", index_col=0)
    logging.info("Input data read.")

    # Time
    timeslice = config['time']['slice']
    time_resolution = config['time']['resolution']
    timestamps = pd.date_range(timeslice[0], timeslice[1], freq=f"{time_resolution}H")

    # Building network
    # Add location to Generators and StorageUnits
    override_comp_attrs = pypsa.descriptors.Dict({k: v.copy() for k, v in pypsa.components.component_attrs.items()})
    override_comp_attrs["Generator"].loc["x"] = ["float", np.nan, np.nan, "x in position (x;y)", "Input (optional)"]
    override_comp_attrs["Generator"].loc["y"] = ["float", np.nan, np.nan, "y in position (x;y)", "Input (optional)"]
    override_comp_attrs["StorageUnit"].loc["x"] = ["float", np.nan, np.nan, "x in position (x;y)", "Input (optional)"]
    override_comp_attrs["StorageUnit"].loc["y"] = ["float", np.nan, np.nan, "y in position (x;y)", "Input (optional)"]

    net = pypsa.Network(name="E-highway network", override_component_attrs=override_comp_attrs)
    net.set_snapshots(timestamps)

    # Adding carriers
    for fuel in fuel_info.index[1:-1]:
        net.add("Carrier", fuel, co2_emissions=fuel_info.loc[fuel, "CO2"])

    # Loading topology
    logger.info("Loading topology.")
    countries = get_subregions(config["region"])
    net = get_topology(net, countries, config["add_offshore"], plot=False)

    # Adding load
    logger.info("Adding load.")
    onshore_bus_indexes = net.buses[net.buses.onshore].index
    load = get_load_from_nuts_codes(
        [eh_clusters.loc[bus_id].codes.split(',') for bus_id in onshore_bus_indexes], net.snapshots)
    load_indexes = "Load " + onshore_bus_indexes
    loads = pd.DataFrame(load.values, index=net.snapshots, columns=load_indexes)
    net.madd("Load", load_indexes, bus=onshore_bus_indexes, p_set=loads)

    # Get peak load and normalized load profile
    loads_max = loads.max(axis=0)
    loads_pu = loads.apply(lambda x: x/x.max(), axis=0)
    # Add generators for load shedding (prevents the model from being infeasible
    net.madd("Generator",
             "Load shed " + onshore_bus_indexes,
             bus=onshore_bus_indexes,
             type="load",
             p_nom=loads_max.values,
             p_max_pu=loads_pu.values,
             x=net.buses.loc[onshore_bus_indexes].x.values,
             y=net.buses.loc[onshore_bus_indexes].y.values,
             marginal_cost=fuel_info.loc["load", "cost"])

    # Adding pv and wind generators
    if config['res']['include']:
        for strategy, technologies in config['res']['strategies'].items():
            # If no technology is associated to this strategy, continue
            if not len(technologies):
                continue

            logger.info(f"Adding RES {technologies} generation with strategy {strategy}.")

            if strategy in ["comp", "max"]:
                net = add_res_from_file(net, technologies, strategy,
                                        config["res"]["path"], config["res"]["area_per_site"],
                                        config["res"]["spatial_resolution"], countries,
                                        topology_type='ehighway', cap_dens_dict=config["res"]["cap_dens"])
            elif strategy == "bus":
                net = add_res_per_bus(net, technologies, countries, pv_wind_tech_config,
                                      config["res"]["use_ex_cap"], topology_type='ehighway')
            elif strategy == "no_siting":
                net = add_res_at_resolution(net, technologies, [config["region"]],
                                            pv_wind_tech_config, config["res"]["spatial_resolution"],
                                            config['res']['filtering_layers'], config["res"]["use_ex_cap"],
                                            topology_type='ehighway')
            elif strategy == 'siting':
                net = add_res(net, technologies, config['res'], pv_wind_tech_config, config["region"],
                              topology_type='ehighway', output_dir=f"{output_dir}resite/")
            # elif config['res']['strategy'] == 'bus_test':
            #    net = add_generators_at_bus_test(net, config['res'], pv_wind_tech_config, config["region"], output_dir)

    # Remove offshore locations that have no RES generators associated to them
    for bus_id in net.buses.index:
        if not net.buses.loc[bus_id].onshore and len(net.generators[net.generators.bus == bus_id]) == 0:
            # Remove the bus
            net.remove("Bus", bus_id)
            # Remove the lines associated to the bus
            # !!!!! Change to links for transportation model -> turn back to line when needed
            net.mremove("Link", net.links[net.links.bus0 == bus_id].index)

    # Add conventional gen
    if config["dispatch"]["include"]:
        tech = config["dispatch"]["tech"]
        net = add_conventional(net, tech)

    # Adding nuclear
    if config["nuclear"]["include"]:
        net = add_nuclear(net, countries, config["nuclear"]["use_ex_cap"], config["nuclear"]["extendable"],
                          "pp_nuclear_WNA.csv")

    if config["sto"]["include"]:
        net = add_sto_plants(net, 'ehighway', config["sto"]["extendable"], config["sto"]["cyclic_sof"])

    if config["phs"]["include"]:
        net = add_phs_plants(net, 'ehighway', config["phs"]["extendable"], config["phs"]["cyclic_sof"])

    if config["ror"]["include"]:
        net = add_ror_plants(net, 'ehighway', config["ror"]["extendable"])

    if config["battery"]["include"]:
        net = add_batteries(net, config["battery"]["type"], config["battery"]["max_hours"])

    co2_reference_kt = \
        get_reference_emission_levels_for_region(config["region"], config["co2_emissions"]["reference_year"])
    co2_budget = co2_reference_kt*(1-config["co2_emissions"]["mitigation_factor"])*len(net.snapshots)/NHoursPerYear
    net.add("GlobalConstraint", "CO2Limit", carrier_attribute="co2_emissions", sense="<=", constant=co2_budget)

    # Compute and save results
    if not isdir(output_dir):
        makedirs(output_dir)
    net.lopf(solver_name=config["solver"], solver_logfile=f"{output_dir}test.log",
             solver_options=config["solver_options"][config["solver"]], pyomo=True)

    # if True:
    #     from pyomo.opt import ProblemFormat
    #     net.model.write(filename=join(output_dir, 'model.lp'),
    #                     format=ProblemFormat.cpxlp,
    #                     io_options={'symbolic_solver_labels': True})

    # Save config and parameters files
    yaml.dump(config, open(f"{output_dir}config.yaml", 'w'))
    yaml.dump(tech_info, open(f"{output_dir}tech_info.yaml", 'w'))
    yaml.dump(fuel_info, open(f"{output_dir}fuel_info.yaml", 'w'))
    yaml.dump(pv_wind_tech_config, open(f"{output_dir}pv_wind_tech_config.yaml", 'w'))

    net.export_to_csv_folder(output_dir)

    # Display some results
    results = SizingResults(net)
    results.display_generation()
    results.display_transmission()
    results.display_storage()
