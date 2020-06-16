from typing import List

from src.postprocessing.utils import *

# Technology name conversion
tech_name_change = {'ccgt': 'CCGT',
                    'wind_offshore': "W_off",
                    'wind_onshore': "W_on",
                    'pv_utility': "PV_util",
                    'pv_residential': "PV_res"}


def generate_costs_table(nets: List[pypsa.Network], names: List[str],
                         technologies: List[str], save: bool = False):
    """Generate a csv table containing various costs of different sizing runs."""

    cost_table = pd.DataFrame(index=pd.MultiIndex.from_product((names, ["CAPEX", "OPEX"])),
                              columns=technologies, dtype=float)
    for i, net in enumerate(nets):
        name = names[i]
        gen_cap_cost, gen_marg_cost = get_gen_capital_and_marginal_cost(net)
        store_cap_cost, store_marg_cost = get_storage_capital_and_marginal_cost(net)
        links_cap_cost = get_links_capex(net)
        links_marg_cost = pd.Series(0, index=links_cap_cost.index)
        cap_cost = pd.concat([gen_cap_cost, links_cap_cost, store_cap_cost]).reindex(technologies).dropna()
        marg_cost = pd.concat([gen_marg_cost, links_marg_cost, store_marg_cost]).reindex(technologies).dropna()
        cost_table.loc[(name, "CAPEX")] = cap_cost
        cost_table.loc[(name, "OPEX")] = marg_cost
        # cost_table.loc[(name, "TOTAL")] = marg_cost + cap_cost
    cost_table = cost_table.round(2)

    # Change columns names
    cost_table = cost_table.rename(columns=tech_name_change)

    if not save:
        return cost_table

    # Change slightly indexes when saving file
    indexes = []
    for name in names:
        indexes += [(name, "CAPEX"), ("", "OPEX")]  #, ("", "TOTAL")]
    cost_table.index = pd.MultiIndex.from_tuples(indexes)
    table.to_csv("table_costs.csv")


def convert_cost_table_to_latex(table):
    text = "\\begin{table}\n" \
           "\centering\n" \
           "\\begin{tabular}{ccr" + "c"*len(table.columns) + "}\n" \
           "\t& OBJ & "
    for name in table.columns:
        name = name.replace("_", "\\textsubscript{")
        name += "}" if "{" in name else ""
        text += f"& {name} "
    text += "\\\\\n"
    text += "\t& $\\times10^5$ & & \multicolumn{" + str(len(table.columns)) + "}{c}{$\\times10^3$}\\\\\midrule\n"
    for i, index in enumerate(table.index):
        case, value_name = index
        value_name = value_name.replace("_", "\\textsubscript{")
        value_name += "}" if "{" in value_name else ""
        if value_name == "CAPEX":
            text += "\multirow{2}{*}{" + case + "} & \multirow{2}{*}{0} & " + value_name
        else:
            text += f"\t& & {value_name}"
        for v in table.loc[index].values:
            text += f" & {v} " if not np.isnan(v) else " & "
        text += "\\\\"
        if value_name == "OPEX" and i != len(table)-1:
            text += "\midrule"
        text += "\n"
    text += "\\bottomrule\n" \
            "\end{tabular}\n" \
            "\caption{Caption}\n" \
            "\label{tab:label}\n" \
            "\end{table}"

    return text


def generate_capacities_table(nets: List[pypsa.Network], names: List[str],
                              technologies: List[str], save: bool = False):
    """Generate a csv table containing capacities and others of different sizing runs."""

    table = pd.DataFrame(index=pd.MultiIndex.from_product((names, ["GW_add", "GW_tot", "GWh", "CF"])),
                         columns=technologies, dtype=float)
    for i, net in enumerate(nets):
        name = names[i]
        # Capacities
        gen_cap = get_generators_capacity(net)
        links_cap = get_links_capacity(net)
        store_cap = get_storage_power_capacity(net)
        new_cap = pd.concat([gen_cap["new"], links_cap["new [TWkm]"],
                             store_cap["new [GW]"]]).reindex(technologies).dropna()
        final_cap = pd.concat([gen_cap["final"], links_cap["init [TWkm]"] + links_cap["new [TWkm]"],
                               store_cap["init [GW]"] + store_cap["new [GW]"]]).reindex(technologies).dropna()
        table.loc[(name, "GW_add")] = new_cap
        table.loc[(name, "GW_tot")] = final_cap

        # Generation
        gen_power = get_generators_generation(net)
        links_power = get_links_power(net)
        power = pd.concat([gen_power, links_power]).reindex(technologies).dropna()
        table.loc[(name, "GWh")] = power

        # Capacity factors
        gen_cf = get_generators_average_usage(net)
        links_cf = get_links_usage(net)
        cf = pd.concat([gen_cf, links_cf]).reindex(technologies).dropna()
        table.loc[(name, "CF")] = cf

    table = table.round(2).abs()

    # Change columns names
    table = table.rename(columns=tech_name_change)

    if not save:
        return table

    # Change slightly indexes
    indexes = []
    for name in names:
        indexes += [(name, "GW_add"), ("", "GW_tot"), ("", "GWh"), ("", "CF")]
    table.index = pd.MultiIndex.from_tuples(indexes)
    table.to_csv("table_capacities.csv")


def convert_cap_table_to_latex(table):
    text = "\\begin{table}\n" \
           "\centering\n" \
           "\\begin{tabular}{cr" + "c"*len(table.columns) + "}\n" \
           "\t&  "
    for name in table.columns:
        name = name.replace("_", "\\textsubscript{")
        name += "}" if "{" in name else ""
        text += f"& {name} "
    text += "\\\\\n"
    text += "\t& "
    for name in table.columns:
        text += "& "
        if name in ["AC", "DC"]:
            text += "(TW x km) "
        if name in ["Li-ion"]:
            text += "(GWh) "
    text += "\\\\\midrule\n"
    for i, index in enumerate(table.index):
        case, value_name = index
        value_name = value_name.replace("_", "\\textsubscript{")
        value_name += "}" if "{" in value_name else ""
        if value_name == "GW\\textsubscript{add}":
            text += "\multirow{4}{*}{" + case + "} & " + value_name
        else:
            text += f"\t & {value_name}"
        for v in table.loc[index].values:
            text += f" & {v} " if not np.isnan(v) else " & "
        text += "\\\\"
        if value_name == "CF" and i != len(table)-1:
            text += "\midrule"
        text += "\n"
    text += "\\bottomrule\n" \
            "\end{tabular}\n" \
            "\caption{Caption}\n" \
            "\label{tab:label}\n" \
            "\end{table}"

    return text


if __name__ == '__main__':

    from pypsa import Network

    topology = 'tyndp2018'
    run_names = ['test_1', 'test_2']
    run_ids = ['20200612_150412', '20200612_151458']
    nets = []
    for run_id in run_ids:
        output_dir = f'../../output/sizing/{topology}/{run_id}/'
        net = Network()
        net.import_from_csv_folder(output_dir)
        nets += [net]

    table = generate_costs_table(nets, run_names, ["ccgt", "wind_offshore", "wind_onshore", "pv_utility",
                                                   "pv_residential", "AC", "DC", "Li-ion"])
    text = convert_cost_table_to_latex(table)
    print(text)

    table = generate_capacities_table(nets, run_names, ["ccgt", "wind_offshore", "wind_onshore", "pv_utility",
                                                        "pv_residential", "AC", "DC", "Li-ion"])
    text = convert_cap_table_to_latex(table)
    print(text)