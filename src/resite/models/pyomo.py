from os.path import join
from typing import List, Dict, Tuple

from numpy import arange
import pandas as pd

from pyomo.environ import ConcreteModel, Var, Constraint, NonNegativeReals, value, Binary
from pyomo.opt import ProblemFormat, SolverFactory

from .pyomo_aux import create_generation_y_dict


# TODO: Still need to replace 'generation_check_rule' for 'meet_demand_with_capacity'
# TODO: create 1 if else with four (or three merging the two last ones) blocks
def build_model(resite, formulation: str, deployment_vector: List[float],
                write_lp: bool = False, output_folder: str = None):
    """
    Model build-up.

    Parameters:
    ------------
    formulation: str
        Formulation of the optimization problem to solve
    deployment_vector: List[float]
        # TODO: this is dependent on the formulation so maybe we should create a different function for each formulation
    write_lp : bool (default: False)
        If True, the model is written to an .lp file.
    output_folder: str
        Place to save the .lp file.
    """

    accepted_formulations = ['meet_RES_targets_agg', 'meet_RES_targets_hourly', 'meet_demand_with_capacity',
                             'maximize_generation', 'maximize_aggr_cap_factor', 'meet_RES_targets_daily',
                             'meet_RES_targets_weekly', 'meet_RES_targets_monthly']
    assert formulation in accepted_formulations, f"Error: formulation {formulation} is not implemented." \
                                                 f"Accepted formulations are {accepted_formulations}."

    load = resite.load_df.values
    tech_points_tuples = [(tech, coord[0], coord[1]) for tech, coord in resite.tech_points_tuples]

    # TODO: need to change this
    intrange = arange(len(resite.timestamps))
    timestamps = resite.timestamps # pd.date_range(resite.timestamps[0], resite.timestamps[-1], freq='H')
    if formulation == 'meet_RES_targets_daily':
        time_slices = [list(intrange[timestamps.dayofyear == day]) for day in timestamps.dayofyear.unique()]
    elif formulation == 'meet_RES_targets_weekly':
        time_slices = [list(intrange[timestamps.weekofyear == week]) for week in timestamps.weekofyear.unique()]
    elif formulation == 'meet_RES_targets_monthly':
        time_slices = [list(intrange[timestamps.month == mon]) for mon in timestamps.month.unique()]
    elif formulation == 'meet_RES_targets_hourly':
        time_slices = [[u] for u in intrange]
    elif formulation == 'meet_RES_targets_agg':
        time_slices = [intrange]
    elif formulation == 'meet_demand_with_capacity':
        time_slices = intrange
    else:
        pass

    model = ConcreteModel()

    if formulation in ['meet_RES_targets_agg', 'meet_RES_targets_hourly', 'meet_RES_targets_daily',
                       'meet_RES_targets_weekly', 'meet_RES_targets_monthly', 'meet_demand_with_capacity']:

        from .pyomo_aux import capacity_bigger_than_existing, minimize_deployed_capacity, \
            generation_bigger_than_load_proportion

        # Variables for the portion of capacity at each location for each technology
        model.y = Var(tech_points_tuples, within=NonNegativeReals, bounds=(0, 1))

        # Create generation dictionary for building speed up
        region_generation_y_dict = create_generation_y_dict(model, resite)

        if formulation.startswith('meet_RES_targets'):  # ['meet_RES_targets_agg', "meet_RES_targets_hourly"]:

            # Impose a certain percentage of the load to be covered over the whole time slice
            covered_load_perc_per_region = dict(zip(resite.regions, deployment_vector))
            model.generation_check = generation_bigger_than_load_proportion(model, region_generation_y_dict, load,
                                                                            resite.regions, time_slices,
                                                                            covered_load_perc_per_region)

            # Percentage of capacity installed must be bigger than existing percentage
            model.potential_constraint = capacity_bigger_than_existing(model, resite.existing_cap_percentage_ds,
                                                                       tech_points_tuples)

            # Minimize the capacity that is deployed
            model.objective = minimize_deployed_capacity(model, resite.cap_potential_ds)

        elif formulation == 'meet_demand_with_capacity':

            from .pyomo_aux import tech_cap_bigger_than_limit, maximize_load_proportion

            # Variables for the portion of demand that is met at each time-stamp for each region
            model.x = Var(resite.regions, time_slices, within=NonNegativeReals, bounds=(0, 1))

            # Generation must be greater than x percent of the load in each region for each time step
            def generation_check_rule(model, region, t):
                return region_generation_y_dict[region][t] >= load[t, resite.regions.index(region)] * model.x[region, t]

            model.generation_check = Constraint(resite.regions, time_slices, rule=generation_check_rule)

            # Percentage of capacity installed must be bigger than existing percentage
            model.potential_constraint = capacity_bigger_than_existing(model, resite.existing_cap_percentage_ds,
                                                                       tech_points_tuples)

            # The capacity installed for each technology must be superior to a certain limit
            required_cap_per_tech = dict(zip(resite.technologies, deployment_vector))
            model.capacity_target = tech_cap_bigger_than_limit(model, resite.cap_potential_ds, resite.tech_points_dict,
                                                               resite.technologies, required_cap_per_tech)

            # Maximize the proportion of load that is satisfied
            model.objective = maximize_load_proportion(model, resite.regions, time_slices)

    elif formulation in ['maximize_generation', 'maximize_aggr_cap_factor']:

        from .pyomo_aux import limit_number_of_sites_per_region, maximize_production

        # Variables for the portion of capacity at each location for each technology
        model.y = Var(tech_points_tuples, within=Binary)
        nb_sites_per_region = dict(zip(resite.regions, deployment_vector))

        if formulation == 'maximize_generation':

            # Maximize generation
            model.policy_target = limit_number_of_sites_per_region(model, resite.regions,
                                                                   resite.region_tech_points_dict, nb_sites_per_region)
            model.objective = maximize_production(model, resite.generation_potential_df, tech_points_tuples)

        elif formulation == 'maximize_aggr_cap_factor':

            # Maximize sum of capacity factors over time slice
            model.policy_target = limit_number_of_sites_per_region(model, resite.regions,
                                                                   resite.region_tech_points_dict, nb_sites_per_region)
            model.objective = maximize_production(model, resite.cap_factor_df, tech_points_tuples)

    if write_lp:
        model.write(filename=join(output_folder, 'model_resite_pyomo.lp'),
                    format=ProblemFormat.cpxlp,
                    io_options={'symbolic_solver_labels': True})

    resite.instance = model


def solve_model(resite):
    """Solve a model."""
    opt = SolverFactory('gurobi')  # TODO: change

    results = opt.solve(resite.instance, tee=True, keepfiles=False, report_timing=False)
    resite.results = results

    return results


def retrieve_solution(resite) -> Tuple[float, Dict[str, List[Tuple[float, float]]], pd.Series]:
    """
    Get the solution of the optimization.

    Returns
    -------
    objective: float
        Objective value after optimization
    selected_tech_points_dict: Dict[str, List[Tuple[float, float]]]
        Lists of points for each technology used in the model
    optimal_capacity_ds: pd.Series
        Gives for each pair of technology-location the optimal capacity obtained via the optimization

    """

    optimal_capacity_ds = pd.Series(index=pd.MultiIndex.from_tuples(resite.tech_points_tuples))
    selected_tech_points_dict = {tech: [] for tech in resite.technologies}

    tech_points_tuples = [(tech, coord[0], coord[1]) for tech, coord in resite.tech_points_tuples]
    for tech, lon, lat in tech_points_tuples:
        y_value = resite.instance.y[tech, (lon, lat)].value
        optimal_capacity_ds[tech, (lon, lat)] = y_value*resite.cap_potential_ds[tech, (lon, lat)]
        if y_value > 0.:
            selected_tech_points_dict[tech] += [(lon, lat)]

    # Remove tech for which no points was selected
    selected_tech_points_dict = {k: v for k, v in selected_tech_points_dict.items() if len(v) > 0}

    # Save objective value
    objective = value(resite.instance.objective)

    return objective, selected_tech_points_dict, optimal_capacity_ds
